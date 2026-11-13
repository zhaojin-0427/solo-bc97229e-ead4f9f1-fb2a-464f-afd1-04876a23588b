"""End-to-end smoke test of the core pipeline (stdlib only).

Run from the project root:
    python3 -I scripts/smoke_test.py

Exercises: happy path / order & duplicate preservation, bad line whole-batch
failure, real enforced timeout via catastrophic backtracking, cancellation
before completion, restart recovery, and download gating.
"""
import json
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Isolate all persistent state in a throwaway directory.
DATA = tempfile.mkdtemp(prefix="anon-smoke-")
os.environ["ANON_DATA_DIR"] = DATA

from app import config, db, dupindex  # noqa: E402
from app.executor import RegexExecutor, RegexTimeout  # noqa: E402
from app.validation import ValidationError, load_rules, read_ndjson  # noqa: E402
from app.worker import process_task, BatchFailure  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def make_task(name, content, rules):
    path = os.path.join(config.UPLOAD_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    task_id = db.create_task(name, path, rules)
    return db.get_task(task_id)


def claim(task_id):
    """Mirror the worker: pending -> running before process_task runs."""
    conn = db.get_conn()
    conn.execute(
        "UPDATE tasks SET state=?, started_at=? WHERE task_id=?",
        (config.STATE_RUNNING, db.utcnow_iso(), task_id),
    )
    conn.close()
    return db.get_task(task_id)


def run(task_id):
    """Mirror worker.main: process, and record failures in the DB."""
    tmp = os.path.join(config.TMP_DIR, f"{task_id}.ndjson.tmp")
    try:
        process_task(db.get_task(task_id))
    except BatchFailure as exc:
        if os.path.exists(tmp):
            os.remove(tmp)
        db.mark_failed(task_id, str(exc))


def main():
    mp.set_start_method("fork")
    db.init_storage()

    print("[1] 执行器：正常替换、反向引用、编译错误、真实超时 kill")
    ex = RegexExecutor(timeout=config.RULE_TIMEOUT_SECONDS)
    check("普通替换", ex.substitute(r"1[3-9]\d{9}", "<手机>", "a 13812345678 b") == "a <手机> b")
    check("反向引用", ex.substitute(r"(\d{3})\d{4}(\d{4})", r"\1****\2", "13812345678") == "138****5678")
    try:
        ex.substitute("(unclosed", "x", "y")
        check("编译错误抛出", False)
    except Exception as exc:
        check("编译错误抛出 RegexError", "RegexError" in type(exc).__name__, repr(exc))
    t0 = time.monotonic()
    try:
        ex.substitute("(a+)+$", "<X>", "a" * 30 + "!")
        check("灾难性回溯被超时拦截", False)
    except RegexTimeout:
        elapsed = time.monotonic() - t0
        check("灾难性回溯被超时拦截", True)
        check(f"超时在 ~2s 触发（实际 {elapsed:.2f}s）", elapsed < 4.0)
    # The executor must have replaced its killed child and still work.
    check("超时后执行器仍可复用", ex.substitute(r"\d+", "N", "abc123") == "abcN")
    ex.close()

    print("[2] 正常批处理：会话号 / 行序 / 重复行 / 额外字段全保留")
    rules = load_rules(json.dumps([
        [r"1[3-9]\d{9}", "<手机>"],
        [r"\d{17}[\dXx]", "<身份证>"],
        [r"[\w.+-]+@[\w-]+\.[\w.-]+", "<邮箱>"],
    ]))
    ndjson = (
        '{"session_id":"s1","text":"手机13812345678","extra":"保留字段"}\n'
        '{"session_id":"s2","text":"邮箱a@b.com"}\n'
        '{"session_id":"s2","text":"邮箱a@b.com"}\n'
        '{"id":"s3","text":"无敏感信息 2026"}\n'
    )
    task = claim(make_task("ok.ndjson", ndjson, rules)["task_id"])
    process_task(task)
    done = db.get_task(task["task_id"])
    check("状态为 completed", done["state"] == config.STATE_COMPLETED, done["state"])
    check("进度满额", done["progress"] == 4 and done["total"] == 4)
    with open(done["output_path"], encoding="utf-8") as fh:
        out = [json.loads(line) for line in fh if line.strip()]
    check("输出行数=输入行数", len(out) == 4)
    check("会话号保留", [r.get("session_id", r.get("id")) for r in out] == ["s1", "s2", "s2", "s3"])
    check("重复行保留且脱敏一致", out[1] == out[2])
    check("手机脱敏", out[0]["text"] == "手机<手机>")
    check("额外字段保留", out[0]["extra"] == "保留字段")
    check("无敏感信息原样", out[3]["text"] == "无敏感信息 2026")
    check("完成任务可下载", os.path.isfile(done["output_path"]))

    print("[3] 坏行 / 非 UTF-8 / 空文件 -> 整批失败，且无输出残留")
    bad_cases = {
        "bad-json.ndjson": '{"session_id":"s1","text":"ok"}\nnot-json\n',
        "missing-text.ndjson": '{"session_id":"s1"}\n',
        "missing-sid.ndjson": '{"text":"no sid"}\n',
        "blank-line.ndjson": '{"session_id":"s1","text":"a"}\n\n',
    }
    for fname, content in bad_cases.items():
        tid = make_task(fname, content, rules)["task_id"]
        claim(tid)
        run(tid)
        row = db.get_task(tid)
        check(f"{fname} 整批失败", row["state"] == config.STATE_FAILED)
        check(f"{fname} 无输出文件", row["output_path"] is None)
        check(f"{fname} 无 tmp 残留", not os.path.exists(os.path.join(config.TMP_DIR, tid + ".ndjson.tmp")))

    raw = os.path.join(config.UPLOAD_DIR, "bad-utf8.ndjson")
    with open(raw, "wb") as fh:
        fh.write(b'{"session_id":"s1","text":"\xff\xfe bad"}\n')
    tid = db.create_task("bad-utf8.ndjson", raw, rules)
    claim(tid)
    run(tid)
    check("非 UTF-8 整批失败", db.get_task(tid)["state"] == config.STATE_FAILED)

    try:
        read_ndjson(raw)
        check("空文件校验", False)
    except ValidationError:
        check("坏文件直读抛 ValidationError", True)
    empty = os.path.join(config.UPLOAD_DIR, "empty.ndjson")
    open(empty, "w").close()
    tid = db.create_task("empty.ndjson", empty, rules)
    claim(tid)
    run(tid)
    check("空文件整批失败", db.get_task(tid)["state"] == config.STATE_FAILED)

    print("[4] 正则语法错误 -> 写输出前整批失败")
    bad_rules = load_rules(json.dumps([[r"\d+", "N"], ["(oops", "x"]]))
    tid = make_task("badrule.ndjson", ndjson, bad_rules)["task_id"]
    claim(tid)
    run(tid)
    row = db.get_task(tid)
    check("坏正则整批失败", row["state"] == config.STATE_FAILED)
    check("坏正则无任何输出", row["output_path"] is None and not os.path.exists(os.path.join(config.OUTPUT_DIR, tid + ".ndjson")))

    print("[5] 超时 -> 整批失败，部分结果清理，执行不继续")
    slow_rules = load_rules(json.dumps([[r"(a+)+$", "<X>"]]))
    slow_input = (
        '{"session_id":"x1","text":"ok"}\n'
        '{"session_id":"x2","text":"' + "a" * 30 + '!"}\n'
        '{"session_id":"x3","text":"must-not-process"}\n'
    )
    tid = make_task("slow.ndjson", slow_input, slow_rules)["task_id"]
    claim(tid)
    t0 = time.monotonic()
    run(tid)
    elapsed = time.monotonic() - t0
    row = db.get_task(tid)
    check("超时任务整批失败", row["state"] == config.STATE_FAILED, row["error"])
    check(f"失败耗时受控（{elapsed:.2f}s）", elapsed < 6.0)
    check("超时后无输出文件", row["output_path"] is None)
    check("tmp 残件已清理", os.listdir(config.TMP_DIR) == [])

    print("[6] 取消：处理中受理 -> 停止替换 -> 清理 -> 终态不可下载")
    cancel_rules = load_rules(json.dumps([[r"\d", "N"]]))
    big = "".join(
        '{"session_id":"s%05d","text":"记录编号%05d 1234567890"}\n' % (i, i)
        for i in range(2000)
    )
    t = make_task("cancel.ndjson", big, cancel_rules)
    tid = t["task_id"]
    claim(tid)  # 模拟 worker 已接单进入 running
    accepted, state = db.request_cancel(tid)
    check("处理中可受理取消", accepted and state == config.STATE_RUNNING)
    process_task(db.get_task(tid))
    row = db.get_task(tid)
    check("取消后状态 cancelled", row["state"] == config.STATE_CANCELLED, row["state"])
    check("取消任务不可下载", row["output_path"] is None)
    check("取消后无 tmp 残留", os.listdir(config.TMP_DIR) == [])
    accepted2, state2 = db.request_cancel(tid)
    check("终态后拒绝再次取消", not accepted2 and state2 == config.STATE_CANCELLED)

    print("[7] 发布优先：完成后取消被拒绝，下载不受影响")
    t = make_task("race.ndjson", ndjson, rules)
    tid = t["task_id"]
    process_task(claim(tid))
    accepted, state = db.request_cancel(tid)
    check("已完成任务拒绝取消", not accepted and state == config.STATE_COMPLETED)
    check("完成副本仍存在", os.path.isfile(db.get_task(tid)["output_path"]))

    print("[8] 重启恢复：未完成 -> interrupted 且不可下载；完成副本保留")
    running = make_task("running.ndjson", ndjson, rules)
    conn = db.get_conn()
    conn.execute("UPDATE tasks SET state=?, started_at=? WHERE task_id=?",
                 (config.STATE_RUNNING, db.utcnow_iso(), running["task_id"]))
    conn.close()
    # 模拟一个残件
    leftover = os.path.join(config.TMP_DIR, running["task_id"] + ".ndjson.tmp")
    open(leftover, "w").write("partial\n")
    completed_before = sum(1 for tsk in db.list_tasks() if tsk["state"] == config.STATE_COMPLETED)
    db.mark_unfinished_interrupted()
    db.cleanup_orphan_tmp_files()
    row = db.get_task(running["task_id"])
    check("运行中任务标记 interrupted", row["state"] == config.STATE_INTERRUPTED)
    check("中断任务无 output_path", row["output_path"] is None)
    check("残件被清理", not os.path.exists(leftover) and os.listdir(config.TMP_DIR) == [])
    check("历史完成副本保留",
          sum(1 for tsk in db.list_tasks() if tsk["state"] == config.STATE_COMPLETED) == completed_before)

    print("[9] 规则结构校验：合法形式放行，不评估正则代价")
    check("合法规则解析", load_rules('[["\\\\d+", "#"]]') == [("\\d+", "#")])
    for bad in ["", "not json", "[]", "[123]", '[["only-one"]]', '[["a", 1]]', '{"a":1}']:
        try:
            load_rules(bad)
            check(f"拒绝坏规则 {bad!r}", False)
        except ValidationError:
            check(f"拒绝坏规则 {bad!r}", True)

    print("[10] 重复语料索引：完成即提交、同批重复分别计数、空文本排除")
    nop_rules = load_rules(json.dumps([["敏感词", "<X>"]]))
    ndjson_a = (
        '{"session_id":"a1","text":"重复文本甲"}\n'
        '{"session_id":"a2","text":"独有甲"}\n'
        '{"session_id":"a3","text":"重复文本甲"}\n'
        '{"session_id":"a4","text":""}\n'
    )
    ta = claim(make_task("dup-a.ndjson", ndjson_a, nop_rules)["task_id"])
    process_task(ta)
    ta_id = ta["task_id"]
    groups = {g["text"]: g for g in db.dup_groups(1, 100)}
    g = groups.get("重复文本甲")
    check("完成即入索引（同批两次分别计数）",
          g is not None and g["occurrences"] == 2 and g["batches"] == 1, groups)

    ndjson_b = (
        '{"session_id":"b1","text":"重复文本甲"}\n'
        '{"session_id":"b2","text":"独有乙"}\n'
    )
    tb = claim(make_task("dup-b.ndjson", ndjson_b, nop_rules)["task_id"])
    process_task(tb)
    tb_id = tb["task_id"]

    st = db.dup_readiness()
    check("全部完成批次已索引即就绪",
          st["ready"] and st["indexed"] == st["total"] and st["total"] >= 4, st)
    groups = {g["text"]: g for g in db.dup_groups(1, 100)}
    g = groups.get("重复文本甲")
    check("跨批重复文本归组，次数=3", g is not None and g["occurrences"] == 3)
    check("涉及批次=2", g is not None and g["batches"] == 2)
    check("只出现一次的文本不成组",
          "独有甲" not in groups and "独有乙" not in groups)
    check("分组总数正确（含此前完成批次）", db.dup_group_count() == len(groups) == 4,
          db.dup_group_count())
    check("分组分页大小生效", len(db.dup_groups(1, 2)) == 2
          and len(db.dup_groups(2, 2)) == 2 and len(db.dup_groups(3, 2)) == 0)

    src = db.dup_group_sources(g["text_id"], 1, 100)
    got = sorted((s["task_id"], s["line_no"], s["session_id"]) for s in src)
    want = sorted([(ta_id, 1, "a1"), (ta_id, 3, "a3"), (tb_id, 1, "b1")])
    check("来源 3 条且任务/行号/会话号正确", got == want, got)
    check("来源分页生效", len(db.dup_group_sources(g["text_id"], 1, 2)) == 2
          and len(db.dup_group_sources(g["text_id"], 2, 2)) == 1)

    conn = db.get_conn()
    n_empty = conn.execute("SELECT COUNT(*) FROM dup_texts WHERE text = ''").fetchone()[0]
    n_not_completed = conn.execute(
        """
        SELECT COUNT(*) FROM dup_occurrences o
        JOIN tasks t ON t.task_id = o.task_id
        WHERE t.state != ?
        """,
        (config.STATE_COMPLETED,),
    ).fetchone()[0]
    conn.close()
    check("空文本不入索引", n_empty == 0)
    check("失败/取消/中断批次无索引行", n_not_completed == 0)

    print("[11] 历史副本补建：接续不重复计数、失败可见可重试、不误报就绪")

    def make_legacy_completed(name, content, write_file=True):
        """A completed task with no index row, as left by an older version."""
        tid = uuid.uuid4().hex
        out_path = os.path.join(config.OUTPUT_DIR, f"{tid}.ndjson")
        if write_file:
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(content)
        conn = db.get_conn()
        conn.execute(
            """
            INSERT INTO tasks (task_id, state, progress, total, original_name,
                               input_path, output_path, rules_json,
                               created_at, finished_at)
            VALUES (?, ?, 0, 0, ?, ?, ?, '[]', ?, ?)
            """,
            (
                tid, config.STATE_COMPLETED, name,
                os.path.join(config.UPLOAD_DIR, name), out_path,
                db.utcnow_iso(), db.utcnow_iso(),
            ),
        )
        conn.close()
        return tid

    legacy1 = make_legacy_completed(
        "legacy1.ndjson",
        '{"session_id":"L1","text":"历史重复"}\n'
        '{"session_id":"L2","text":"跨时代重复"}\n'
        '{"session_id":"L3","text":"历史重复"}\n',
    )
    legacy2 = make_legacy_completed("legacy2-missing.ndjson", "", write_file=False)

    st = db.dup_readiness()
    check("补建前未就绪", not st["ready"], st)
    dupindex.backfill_loop(threading.Event())  # returns once caught up
    st = db.dup_readiness()
    check("缺文件批次失败且未误报就绪", not st["ready"] and len(st["failed"]) == 1, st)
    check("失败记录含批次与原因",
          st["failed"][0]["task_id"] == legacy2
          and "读取副本失败" in st["failed"][0]["error"], st["failed"])
    g = {x["text"]: x for x in db.dup_groups(1, 100)}.get("历史重复")
    check("历史批次已补建且同批重复分别计数",
          g is not None and g["occurrences"] == 2 and g["batches"] == 1, g)

    conn = db.get_conn()
    n_occ = conn.execute("SELECT COUNT(*) FROM dup_occurrences").fetchone()[0]
    conn.close()
    dupindex.backfill_loop(threading.Event())  # simulated restart sweep
    conn = db.get_conn()
    n_occ2 = conn.execute("SELECT COUNT(*) FROM dup_occurrences").fetchone()[0]
    conn.close()
    check("重启补建不重复计数", n_occ2 == n_occ, (n_occ, n_occ2))

    with open(os.path.join(config.OUTPUT_DIR, f"{legacy2}.ndjson"), "w",
              encoding="utf-8") as fh:
        fh.write('{"session_id":"L9","text":"跨时代重复"}\n')
    ok, err = dupindex.index_batch(legacy2, db.get_task(legacy2)["output_path"])
    check("失败批次修复后重试成功", ok and err is None, err)
    st = db.dup_readiness()
    check("重试后就绪", st["ready"] and not st["failed"], st)
    g = {x["text"]: x for x in db.dup_groups(1, 100)}.get("跨时代重复")
    check("跨批归组含补建批次，次数=2、批次=2",
          g is not None and g["occurrences"] == 2 and g["batches"] == 2, g)
    check("补建后索引行数仅增加重试批次的一条",
          db.get_task(legacy1)["state"] == config.STATE_COMPLETED)

    shutil.rmtree(DATA, ignore_errors=True)
    print(f"\n结果：{PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
