"""Web-layer test driving the real Flask app object through a WSGI call.

Flask is unavailable in this sandbox, so a small compatible shim
(scripts/flask_shim) supplies the imported names. The application code under
test (app/server/app.py, templates, download gating, error handlers) is the
real thing; production uses genuine Flask pinned in requirements.txt.
"""
import io
import json
import os
import sys
import tempfile
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts", "flask_shim"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import multipart_encoder as mpenc  # noqa: E402

DATA_DIR = tempfile.mkdtemp(prefix="anon-web-")
os.environ["ANON_DATA_DIR"] = DATA_DIR

from app import config, db  # noqa: E402
from app.server.app import create_app  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


COOKIE_JAR = {}


def call(app, method, path, fields=None, files=None, raw_len_override=None):
    """Invoke the app via its WSGI entry and return (status, headers, body)."""
    fields = fields or {}
    files = files or {}
    if files or fields:
        body, ctype = mpenc.encode(fields, files)
    else:
        body, ctype = b"", "application/x-www-form-urlencoded"
    cookie_header = "; ".join(f"{k}={v}" for k, v in COOKIE_JAR.items())
    env = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "CONTENT_TYPE": ctype,
        "CONTENT_LENGTH": str(raw_len_override if raw_len_override else len(body)),
        "SERVER_NAME": "test",
        "SERVER_PORT": "80",
        "wsgi.input": io.BytesIO(body),
    }
    if cookie_header:
        env["HTTP_COOKIE"] = cookie_header
    data, status, headers = app.run_wsgi(env, body)
    # Persist any Set-Cookie for the next request (flash survives redirect).
    sc = headers.get("Set-Cookie")
    if sc:
        pair = sc.split(";", 1)[0]
        k, v = pair.split("=", 1)
        COOKIE_JAR[k] = v
    return status, headers, data


def main():
    db.init_storage()
    app = create_app()

    print("[W1] 首页渲染：含上传表单与空任务列表")
    status, _, body = call(app, "GET", "/")
    text = body.decode("utf-8")
    check("首页 200", status == 200, str(status))
    check("含文件上传控件", 'name="file"' in text)
    check("含规则文本框", 'name="rules"' in text)
    check("空列表提示", "暂无任务" in text)

    print("[W2] 上传合法 NDJSON -> 302 跳转到任务详情，任务入队")
    ndjson = '{"session_id":"w1","text":"手机13812345678"}\n'
    rules = json.dumps([[r"1[3-9]\d{9}", "<手机>"]])
    status, headers, _ = call(
        app, "POST", "/tasks",
        fields={"rules": rules},
        files={"file": ("hotline.ndjson", ndjson.encode("utf-8"))},
    )
    check("上传后 302", status == 302, str(status))
    loc = headers.get("Location", "")
    check("跳转到 /tasks/<id>", loc.startswith("/tasks/") and len(loc.split("/")[-1]) == 32, loc)
    tid = loc.rsplit("/", 1)[-1]
    row = db.get_task(tid)
    check("任务已入库且 pending", row is not None and row["state"] == config.STATE_PENDING)
    check("上传原件已落盘", os.path.isfile(row["input_path"]))

    print("[W3] 任务详情页与 JSON 进度 API")
    status, _, body = call(app, "GET", f"/tasks/{tid}")
    check("详情页 200", status == 200)
    check("详情页含任务号", tid in body.decode())
    status, _, body = call(app, "GET", f"/api/tasks/{tid}")
    check("API 200", status == 200)
    payload = json.loads(body)
    check("API 字段完整",
          payload["task"]["task_id"] == tid and payload["task"]["state"] == "pending"
          and payload["task"]["downloadable"] is False)
    status, _, body = call(app, "GET", "/api/tasks")
    check("列表 API 含该任务", any(t["task_id"] == tid for t in json.loads(body)["tasks"]))

    print("[W4] 未完成任务禁止下载（403），未知任务 404")
    status, _, _ = call(app, "GET", f"/tasks/{tid}/download")
    check("pending 下载被拒 403", status == 403, str(status))
    status, _, _ = call(app, "GET", "/tasks/deadbeef/download")
    check("未知任务 404", status == 404, str(status))
    status, _, _ = call(app, "POST", "/tasks/deadbeef/cancel")
    check("取消未知任务 404", status == 404, str(status))

    print("[W5] 规则非法 -> 闪存错误并重定向，不创建任务")
    before = len(db.list_tasks())
    status, headers, body = call(
        app, "POST", "/tasks",
        fields={"rules": "not-json"},
        files={"file": ("x.ndjson", ndjson.encode())},
    )
    check("非法规则 302 回首页", status == 302 and headers.get("Location") == "/")
    check("未新增任务", len(db.list_tasks()) == before)
    # flash is rendered on the redirected GET
    status, _, body = call(app, "GET", "/")
    check("首页展示错误闪存", "规则不是合法 JSON" in body.decode())

    print("[W6] 缺少文件 -> 闪存提示，不创建任务")
    before = len(db.list_tasks())
    status, _, _ = call(app, "POST", "/tasks", fields={"rules": rules}, files={})
    check("缺文件 302", status == 302)
    check("未新增任务", len(db.list_tasks()) == before)

    print("[W7] 超过 5 MiB -> 413 专用错误页")
    big = b'{"session_id":"big","text":"x"}\n'
    # Claim an over-limit CONTENT_LENGTH without actually sending that many
    # bytes, exactly as the WSGI server reports an oversized request.
    status, _, body = call(
        app, "POST", "/tasks",
        fields={"rules": rules},
        files={"file": ("big.ndjson", big)},
        raw_len_override=config.MAX_CONTENT_LENGTH + 1,
    )
    check("超限返回 413", status == 413, str(status))
    check("413 页面提示 5 MiB", "5 MiB" in body.decode())

    print("[W8] 完成后下载完整副本；完成后拒绝取消")
    # Drive the real processing pipeline, then verify the web download path.
    from app.worker import process_task  # noqa: E402
    conn = db.get_conn()
    conn.execute("UPDATE tasks SET state=? WHERE task_id=?",
                 (config.STATE_RUNNING, tid))
    conn.close()
    process_task(db.get_task(tid))
    done = db.get_task(tid)
    check("任务完成", done["state"] == config.STATE_COMPLETED)
    status, headers, body = call(app, "GET", f"/tasks/{tid}/download")
    check("下载 200", status == 200, str(status))
    check("附件文件名脱敏后缀", "anonymized.ndjson" in headers.get("Content-Disposition", ""))
    out = [json.loads(x) for x in body.decode().splitlines() if x.strip()]
    check("下载内容已脱敏且保留会话号",
          len(out) == 1 and out[0]["session_id"] == "w1" and out[0]["text"] == "手机<手机>")
    status, _, _ = call(app, "POST", f"/tasks/{tid}/cancel")
    check("完成后取消被拒（仍 302 但状态不变）", status == 302
          and db.get_task(tid)["state"] == config.STATE_COMPLETED)

    print("[W9] 失败 / 中断任务同样不可下载")
    fpath = os.path.join(config.UPLOAD_DIR, "fail.ndjson")
    open(fpath, "w").write("not-a-json-line\n")
    ftid = db.create_task("fail.ndjson", fpath, json.loads(rules))
    conn = db.get_conn()
    conn.execute("UPDATE tasks SET state=? WHERE task_id=?", (config.STATE_RUNNING, ftid))
    conn.close()
    from app.worker import BatchFailure
    try:
        process_task(db.get_task(ftid))
    except BatchFailure:
        db.mark_failed(ftid, "test failure")
    check("失败任务落库", db.get_task(ftid)["state"] == config.STATE_FAILED)
    status, _, _ = call(app, "GET", f"/tasks/{ftid}/download")
    check("失败任务下载 403", status == 403)

    print("[W10] 列表页与详情页状态徽标渲染（已完成/失败）")
    status, _, body = call(app, "GET", "/")
    text = body.decode()
    check("列表含已完成徽标", "已完成" in text and "已失败" in text)
    check("完成任务出现下载按钮", "下载完整副本" in text)

    print(f"\n结果：{PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
