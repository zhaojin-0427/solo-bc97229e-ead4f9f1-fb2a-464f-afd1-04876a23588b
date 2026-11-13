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
    query = ""
    if "?" in path:
        path, query = path.split("?", 1)
    cookie_header = "; ".join(f"{k}={v}" for k, v in COOKIE_JAR.items())
    env = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
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

    print("[W11] 重复核查页：索引未就绪时不返回残缺结果")
    import threading
    import uuid
    from app import dupindex

    def make_legacy_completed(name, content, write_file=True):
        lid = uuid.uuid4().hex
        out_path = os.path.join(config.OUTPUT_DIR, f"{lid}.ndjson")
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
            (lid, config.STATE_COMPLETED, name,
             os.path.join(config.UPLOAD_DIR, name), out_path,
             db.utcnow_iso(), db.utcnow_iso()),
        )
        conn.close()
        return lid, out_path

    legacy1, legacy1_path = make_legacy_completed(
        "legacy-web.ndjson",
        '{"session_id":"w9a","text":"网页重复文本"}\n'
        '{"session_id":"w9a","text":"网页重复文本"}\n'
        '{"session_id":"w9b","text":"网页独有"}\n',
    )
    with open(legacy1_path, encoding="utf-8") as fh:
        copy_before = fh.read()

    status, _, body = call(app, "GET", "/duplicates")
    text = body.decode()
    check("未就绪页 200", status == 200, str(status))
    check("提示尚未就绪", "尚未就绪" in text)
    check("不展示分组结果", "查看来源" not in text and "网页重复文本" not in text)
    status, _, body = call(app, "GET", "/api/duplicates/status")
    payload = json.loads(body)
    check("状态 API 未就绪", status == 200 and payload["ready"] is False)
    check("状态含进度", payload["indexed"] >= 1 and payload["total"] >= 2, payload)
    status, _, body = call(app, "GET", "/duplicates/1")
    check("未就绪时组详情同样不返回结果", status == 200 and "尚未就绪" in body.decode())

    print("[W12] 补建后就绪：分组、来源、分页、404、副本只读")
    dupindex.backfill_loop(threading.Event())
    status, _, body = call(app, "GET", "/duplicates")
    text = body.decode()
    check("就绪后 200 且展示分组", status == 200 and "网页重复文本" in text)
    check("分组含次数与查看来源", "查看来源" in text and ">2<" in text)
    check("单次文本不成组", "网页独有" not in text)
    status, _, body = call(app, "GET", "/api/duplicates/status")
    check("状态 API 就绪", json.loads(body)["ready"] is True)
    with open(legacy1_path, encoding="utf-8") as fh:
        check("索引不修改副本", fh.read() == copy_before)

    conn = db.get_conn()
    row = conn.execute(
        "SELECT text_id FROM dup_texts WHERE text = '网页重复文本'"
    ).fetchone()
    dup_text_id = row["text_id"]
    singleton_id = conn.execute(
        "SELECT text_id FROM dup_texts WHERE text = '网页独有'"
    ).fetchone()["text_id"]
    conn.close()

    status, _, body = call(app, "GET", f"/duplicates/{dup_text_id}")
    text = body.decode()
    check("组详情 200", status == 200, str(status))
    check("详情含全文与次数", "网页重复文本" in text and "2 次" in text)
    check("来源含会话号与行号", "w9a" in text and ">1<" in text and ">2<" in text)
    check("来源含任务链接与源文件", legacy1[:12] in text and "legacy-web.ndjson" in text)

    old_size = config.DUP_SOURCE_PAGE_SIZE
    config.DUP_SOURCE_PAGE_SIZE = 1
    try:
        status, _, body = call(app, "GET", f"/duplicates/{dup_text_id}?page=2")
        text = body.decode()
        check("来源分页第二页 200", status == 200 and "第 2 / 2 页" in text)
        check("分页只含一条来源", text.count("w9a") >= 1 and ">1<" not in text)
        status, _, body = call(app, "GET", f"/duplicates/{dup_text_id}?page=99")
        check("超界页码收敛到末页", "第 2 / 2 页" in body.decode())
    finally:
        config.DUP_SOURCE_PAGE_SIZE = old_size

    status, _, _ = call(app, "GET", f"/duplicates/{singleton_id}")
    check("单次文本组详情 404", status == 404, str(status))
    status, _, _ = call(app, "GET", "/duplicates/999999")
    check("未知分组 404", status == 404, str(status))
    status, _, _ = call(app, "POST", "/duplicates/deadbeef/retry")
    check("重试未知任务 404", status == 404, str(status))
    status, headers, _ = call(app, "POST", f"/duplicates/{ftid}/retry")
    check("非完成批次重试仅提示", status == 302)
    conn = db.get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM dup_index_batches WHERE task_id = ?", (ftid,)
    ).fetchone()[0]
    conn.close()
    check("失败批次不产生索引记录", n == 0)

    print("[W13] 读取失败：页面显示批次与原因，可重试，不误报就绪")
    legacy2, legacy2_path = make_legacy_completed(
        "legacy-missing.ndjson", "", write_file=False
    )
    dupindex.backfill_loop(threading.Event())
    status, _, body = call(app, "GET", "/duplicates")
    text = body.decode()
    check("失败时回到未就绪页", status == 200 and "尚未就绪" in text)
    check("页面显示失败批次与原因",
          legacy2[:12] in text and "读取副本失败" in text)
    check("页面提供重试按钮", "重试" in text)

    status, headers, _ = call(app, "POST", f"/duplicates/{legacy2}/retry")
    check("重试后重定向", status == 302 and headers.get("Location") == "/duplicates")
    status, _, body = call(app, "GET", headers.get("Location", "/duplicates"))
    check("缺文件重试仍失败且显示原因", "读取副本失败" in body.decode())

    with open(legacy2_path, "w", encoding="utf-8") as fh:
        fh.write('{"session_id":"w9c","text":"网页重复文本"}\n')
    status, headers, _ = call(app, "POST", f"/duplicates/{legacy2}/retry")
    check("修复后重试成功", status == 302)
    status, _, body = call(app, "GET", "/duplicates")
    text = body.decode()
    check("重试后就绪并展示分组", "尚未就绪" not in text and "查看来源" in text)
    status, _, body = call(app, "GET", f"/duplicates/{dup_text_id}")
    text = body.decode()
    check("跨批来源合入同一组（3 次）", "3 次" in text and "w9c" in text)

    print(f"\n结果：{PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
