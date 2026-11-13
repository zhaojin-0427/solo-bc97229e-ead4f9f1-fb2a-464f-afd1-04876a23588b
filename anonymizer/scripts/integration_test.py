"""Subprocess-level integration test: the real worker module, real DB, real
multiprocessing regex children. Stdlib only.

    python3 -I scripts/integration_test.py

The web layer (Flask) is exercised in Docker; here we drive the same DB the
web app writes to, so the worker contract is tested end to end.
"""
import atexit
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# config reads ANON_DATA_DIR at import time, so the env var must exist first.
DATA_DIR = tempfile.mkdtemp(prefix="anon-integ-")
os.environ["ANON_DATA_DIR"] = DATA_DIR

from app import config, db  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def wait_state(task_id, states, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = db.get_task(task_id)
        if row and row["state"] in states:
            return row
        time.sleep(0.1)
    return db.get_task(task_id)


def wait_removed(path, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not os.path.exists(path):
            return True
        time.sleep(0.1)
    return not os.path.exists(path)


def count_worker_procs():
    """Count live OS processes whose argv is exactly `python -m app.worker`.

    The regex subprocesses spawned by a running worker have a different argv
    (multiprocessing spawn/fork entry point), so they are not counted; neither
    is a shell whose command line merely contains the string "app.worker".
    """
    n = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or pid == str(os.getpid()):
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = [p.decode() for p in fh.read().split(b"\0") if p]
        except OSError:
            continue
        if len(argv) == 3 and argv[1] == "-m" and argv[2] == "app.worker":
            n += 1
    return n


def stop_worker(proc):
    """Best-effort shutdown of a worker process group, used also at exit."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=25)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def start_worker(data_dir):
    env = dict(os.environ, ANON_DATA_DIR=data_dir, PYTHONPATH=ROOT)
    # New session => own process group, so signals reach the worker and its
    # forked regex child together, like a container stop.
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.worker"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    # Guarantee no orphaned worker survives an aborted test run.
    atexit.register(stop_worker, proc)
    return proc


def main():
    db.init_storage()

    rules_phone = json.dumps([[r"1[3-9]\d{9}", "<手机>"]])
    rules_digit = json.dumps([[r"\d", "N"]])

    print("[A] 真实 worker 子进程：接单 -> 完成 -> 落盘副本")
    worker = start_worker(DATA_DIR)
    input_path = os.path.join(config.UPLOAD_DIR, "in-a.ndjson")
    with open(input_path, "w", encoding="utf-8") as fh:
        for i in range(500):
            fh.write(
                '{"session_id":"s%04d","text":"号码138%08d 序号%d"}\n'
                % (i % 50, i, i)
            )
    tid_a = db.create_task("in-a.ndjson", input_path, json.loads(rules_phone))
    row = wait_state(tid_a, config.TERMINAL_STATES)
    check("任务完成", row["state"] == config.STATE_COMPLETED, row["state"])
    check("进度=总数", row["progress"] == row["total"] == 500)
    check("输出副本存在", os.path.isfile(row["output_path"]))
    with open(row["output_path"], encoding="utf-8") as fh:
        lines = [json.loads(x) for x in fh if x.strip()]
    check("输出 500 行且行序/会话号保留",
          len(lines) == 500 and lines[0]["session_id"] == "s0000"
          and lines[499]["session_id"] == "s0049")
    check("全部手机号已脱敏", all("<手机>" in x["text"] for x in lines))
    check("无 tmp 残留", os.listdir(config.TMP_DIR) == [])

    print("[B] 运行中取消：受理 -> 实际停止替换 -> 清理 -> 不可下载")
    big_path = os.path.join(config.UPLOAD_DIR, "in-b.ndjson")
    with open(big_path, "w", encoding="utf-8") as fh:
        for i in range(200000):
            fh.write('{"session_id":"b%06d","text":"%010d"}\n' % (i, i))
    tid_b = db.create_task("in-b.ndjson", big_path, json.loads(rules_digit))
    row = wait_state(tid_b, (config.STATE_RUNNING,), timeout=15.0)
    check("任务进入 running", row is not None and row["state"] == config.STATE_RUNNING)
    # Let some real replacements happen before cancelling.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if db.get_task(tid_b)["progress"] > 100:
            break
        time.sleep(0.1)
    check("已有处理进度", db.get_task(tid_b)["progress"] > 100)
    accepted, state = db.request_cancel(tid_b)
    check("取消请求被受理", accepted and state == config.STATE_RUNNING)
    row = wait_state(tid_b, (config.STATE_CANCELLED,), timeout=30.0)
    check("终态 cancelled", row["state"] == config.STATE_CANCELLED, row["state"])
    check("取消任务无 output_path", row["output_path"] is None)
    check("取消后 tmp 残件清理", wait_removed(
        os.path.join(config.TMP_DIR, tid_b + ".ndjson.tmp")))
    check("outputs 中无该任务文件",
          not os.path.exists(os.path.join(config.OUTPUT_DIR, tid_b + ".ndjson")))

    print("[C] 超时任务失败后，同一 worker 仍能处理后续任务（无拒绝合法正则）")
    slow_path = os.path.join(config.UPLOAD_DIR, "in-c.ndjson")
    with open(slow_path, "w", encoding="utf-8") as fh:
        fh.write('{"session_id":"c1","text":"ok"}\n')
        fh.write('{"session_id":"c2","text":"' + "a" * 30 + '!"}\n')
    tid_c = db.create_task("in-c.ndjson", slow_path,
                           json.loads(json.dumps([[r"(a+)+$", "<X>"]])))
    row = wait_state(tid_c, (config.STATE_FAILED,), timeout=15.0)
    check("超时整批失败", row["state"] == config.STATE_FAILED and "超时" in (row["error"] or ""))
    check("失败任务无副本", row["output_path"] is None)
    next_path = os.path.join(config.UPLOAD_DIR, "in-c2.ndjson")
    with open(next_path, "w", encoding="utf-8") as fh:
        fh.write('{"session_id":"c3","text":"tail 13900001111"}\n')
    tid_c2 = db.create_task("in-c2.ndjson", next_path, json.loads(rules_phone))
    row = wait_state(tid_c2, (config.STATE_COMPLETED,), timeout=15.0)
    check("超时后队列恢复，新任务完成", row["state"] == config.STATE_COMPLETED, row["state"])
    with open(row["output_path"], encoding="utf-8") as fh:
        check("新任务结果正确", '"tail <手机>"' in fh.read())
    check("无孤儿 worker 进程累积", count_worker_procs() == 1,
          f"实际 {count_worker_procs()}")

    print("[D] worker 被 SIGTERM 杀掉 -> 重启恢复：中断不可下载，历史副本保留")
    big2 = os.path.join(config.UPLOAD_DIR, "in-d.ndjson")
    with open(big2, "w", encoding="utf-8") as fh:
        for i in range(300000):
            fh.write('{"session_id":"d%06d","text":"%010d"}\n' % (i, i))
    tid_d = db.create_task("in-d.ndjson", big2, json.loads(rules_digit))
    wait_state(tid_d, (config.STATE_RUNNING,), timeout=15.0)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if db.get_task(tid_d)["progress"] > 100:
            break
        time.sleep(0.1)
    stop_worker(worker)
    # Simulate the container coming back up.
    worker2 = start_worker(DATA_DIR)
    row = wait_state(tid_d, (config.STATE_INTERRUPTED,), timeout=20.0)
    check("未完成任务标记 interrupted", row["state"] == config.STATE_INTERRUPTED, row["state"])
    check("中断任务不可下载", row["output_path"] is None)
    check("中断后 tmp 残件被清理", wait_removed(
        os.path.join(config.TMP_DIR, tid_d + ".ndjson.tmp")))
    check("历史完成副本跨重启保留",
          os.path.isfile(db.get_task(tid_a)["output_path"])
          and os.path.isfile(db.get_task(tid_c2)["output_path"]))

    # And the restarted worker still serves new work.
    e_path = os.path.join(config.UPLOAD_DIR, "in-e.ndjson")
    with open(e_path, "w", encoding="utf-8") as fh:
        fh.write('{"session_id":"e1","text":"13711112222"}\n')
    tid_e = db.create_task("in-e.ndjson", e_path, json.loads(rules_phone))
    row = wait_state(tid_e, (config.STATE_COMPLETED,), timeout=15.0)
    check("重启后新任务正常完成", row["state"] == config.STATE_COMPLETED, row["state"])

    print("[E] 排队状态下被取消：worker 扫到后直接 cancelled，绝不发布")
    # Pause the worker by saturating it is hard; instead set cancel on a
    # pending row while worker is busy, then verify terminal handling.
    f_path = os.path.join(config.UPLOAD_DIR, "in-f.ndjson")
    with open(f_path, "w", encoding="utf-8") as fh:
        fh.write('{"session_id":"f1","text":"x"}\n')
    # Insert two tasks; cancel the second while the first occupies the worker.
    blocker_path = os.path.join(config.UPLOAD_DIR, "blocker.ndjson")
    with open(blocker_path, "w", encoding="utf-8") as fh:
        for i in range(20000):
            fh.write('{"session_id":"z%06d","text":"%010d"}\n' % (i, i))
    tid_block = db.create_task("blocker.ndjson", blocker_path, json.loads(rules_digit))
    tid_f = db.create_task("in-f.ndjson", f_path, json.loads(rules_phone))
    wait_state(tid_block, (config.STATE_RUNNING,), timeout=15.0)
    accepted, state = db.request_cancel(tid_f)  # still pending
    check("排队任务可受理取消", accepted and state == config.STATE_PENDING)
    # Free the single worker so the queued cancellation is settled promptly.
    db.request_cancel(tid_block)
    row = wait_state(tid_block, (config.STATE_CANCELLED,), timeout=60.0)
    check("阻塞任务已取消", row["state"] == config.STATE_CANCELLED)
    row = wait_state(tid_f, (config.STATE_CANCELLED,), timeout=30.0)
    check("排队取消生效且未发布",
          row["state"] == config.STATE_CANCELLED and row["output_path"] is None)

    stop_worker(worker2)
    time.sleep(0.5)
    check("退出后无 worker 进程残留", count_worker_procs() == 0,
          f"实际 {count_worker_procs()}")

    print(f"\n结果：{PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
