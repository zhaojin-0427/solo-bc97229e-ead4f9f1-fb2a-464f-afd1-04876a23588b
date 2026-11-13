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
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Isolate all persistent state in a throwaway directory.
DATA = tempfile.mkdtemp(prefix="anon-smoke-")
os.environ["ANON_DATA_DIR"] = DATA

from app import config, db  # noqa: E402
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

    shutil.rmtree(DATA, ignore_errors=True)
    print(f"\n结果：{PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
