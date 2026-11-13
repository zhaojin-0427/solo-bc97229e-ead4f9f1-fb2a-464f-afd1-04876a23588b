"""Flask web entry point: upload, progress, cancel, download, duplicate check."""
import os
import uuid

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from .. import config, db, dupindex
from ..validation import ValidationError, load_rules


def _page_param():
    try:
        return max(1, int(request.args.get("page", "1")))
    except (TypeError, ValueError):
        return 1


def _paged(total, size, page):
    """Clamp ``page`` into [1, pages] and return (page, pages)."""
    pages = max(1, -(-total // size))
    return min(max(1, page), pages), pages


def _snippet(text, limit=120):
    return text if len(text) <= limit else text[:limit] + "…"


def create_app():
    app = Flask(__name__)
    # Reject uploads larger than 5 MiB at the WSGI layer.
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH
    app.config["SECRET_KEY"] = os.environ.get(
        "ANON_SECRET_KEY", "dev-only-secret-change-me"
    )

    # Table creation is idempotent and safe in every web process. Restart
    # recovery deliberately does NOT run here: the web and worker are separate
    # containers, so a web restart must never touch a task the live worker is
    # processing. Recovery belongs exclusively to worker startup.
    db.init_storage()

    # ------------------------------------------------------------------ pages
    @app.route("/")
    def index():
        return render_template("index.html", tasks=db.list_tasks())

    @app.route("/tasks/<task_id>")
    def task_detail(task_id):
        task = db.get_task(task_id)
        if task is None:
            abort(404)
        return render_template("task.html", task=task)

    # ------------------------------------------------------------------ APIs
    @app.route("/api/tasks")
    def api_tasks():
        return jsonify({"tasks": [_public(t) for t in db.list_tasks()]})

    @app.route("/api/tasks/<task_id>")
    def api_task(task_id):
        task = db.get_task(task_id)
        if task is None:
            return jsonify({"error": "任务不存在"}), 404
        return jsonify({"task": _public(task)})

    @app.post("/tasks")
    def create_task():
        upload = request.files.get("file")
        if upload is None or upload.filename == "":
            flash("请选择要上传的 NDJSON 文件", "error")
            return redirect(url_for("index"))

        try:
            rules = load_rules(request.form.get("rules"))
        except ValidationError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))

        original_name = os.path.basename(upload.filename)
        # Pre-allocate the id and persist the bytes first. Disk names are task
        # ids (never user-controlled), and a failed save produces no orphan
        # task row.
        task_id = uuid.uuid4().hex
        input_path = os.path.join(config.UPLOAD_DIR, f"{task_id}.ndjson")
        try:
            upload.save(input_path)
        except OSError as exc:
            flash(f"上传文件保存失败：{exc}", "error")
            return redirect(url_for("index"))

        task_id = db.create_task(original_name, input_path, rules, task_id=task_id)
        return redirect(url_for("task_detail", task_id=task_id))

    @app.post("/tasks/<task_id>/cancel")
    def cancel_task(task_id):
        accepted, state = db.request_cancel(task_id)
        if state is None:
            abort(404)
        if not accepted:
            # The task already reached a terminal state; if it published first
            # the cancellation must be refused rather than retroactively
            # removing the result.
            flash(f"任务已处于终态（{state}），无法取消", "error")
        return redirect(url_for("task_detail", task_id=task_id))

    @app.route("/tasks/<task_id>/download")
    def download_task(task_id):
        task = db.get_task(task_id)
        if task is None:
            abort(404)
        # Only fully completed batches are downloadable; interrupted, failed,
        # cancelled and still-running tasks never expose a partial copy.
        if task["state"] != config.STATE_COMPLETED:
            abort(403, description="任务未完成或已失败，没有可下载的副本")
        path = task["output_path"]
        if not path or not os.path.isfile(path):
            abort(404, description="结果文件缺失")

        stem, _ext = os.path.splitext(task["original_name"])
        download_name = f"{stem}.anonymized.ndjson"
        return send_file(
            path,
            as_attachment=True,
            download_name=download_name,
            mimetype="application/x-ndjson; charset=utf-8",
        )

    # ---------------------------------------------------- duplicate check
    @app.route("/duplicates")
    def duplicates():
        status = db.dup_readiness()
        for f in status["failed"]:
            f["task_short"] = f["task_id"][:12]
        if not status["ready"]:
            # Never serve partial check results: while any completed batch
            # is unindexed or failed, only the status is shown.
            pct = int(100 * status["indexed"] / status["total"]) if status["total"] else 100
            return render_template(
                "duplicates.html", ready=False, status=status, pct=pct
            )
        size = config.DUP_GROUP_PAGE_SIZE
        total = db.dup_group_count()
        page, pages = _paged(total, size, _page_param())
        groups = db.dup_groups(page, size)
        for g in groups:
            g["snippet"] = _snippet(g["text"])
        return render_template(
            "duplicates.html",
            ready=True,
            status=status,
            groups=groups,
            total=total,
            page=page,
            pages=pages,
            prev_page=page - 1,
            next_page=page + 1,
        )

    @app.route("/duplicates/<int:text_id>")
    def duplicate_group(text_id):
        status = db.dup_readiness()
        if not status["ready"]:
            for f in status["failed"]:
                f["task_short"] = f["task_id"][:12]
            pct = int(100 * status["indexed"] / status["total"]) if status["total"] else 100
            return render_template(
                "duplicates.html", ready=False, status=status, pct=pct
            )
        group = db.dup_group(text_id)
        if group is None or group["occurrences"] < 2:
            abort(404)
        size = config.DUP_SOURCE_PAGE_SIZE
        page, pages = _paged(group["occurrences"], size, _page_param())
        sources = db.dup_group_sources(text_id, page, size)
        for i, s in enumerate(sources):
            s["seq"] = (page - 1) * size + i + 1
            s["task_short"] = s["task_id"][:12]
        return render_template(
            "duplicate_group.html",
            group=group,
            sources=sources,
            page=page,
            pages=pages,
            prev_page=page - 1,
            next_page=page + 1,
        )

    @app.post("/duplicates/<task_id>/retry")
    def retry_duplicate_index(task_id):
        task = db.get_task(task_id)
        if task is None:
            abort(404)
        if task["state"] != config.STATE_COMPLETED:
            flash("只有已完成批次才能重建重复语料索引", "error")
            return redirect(url_for("duplicates"))
        ok, error = dupindex.index_batch(task_id, task["output_path"])
        if ok:
            flash(f"批次 {task_id[:12]}… 的重复语料索引已重建", "message")
        else:
            flash(f"批次 {task_id[:12]}… 重建失败：{error}", "error")
        return redirect(url_for("duplicates"))

    @app.route("/api/duplicates/status")
    def api_duplicates_status():
        return jsonify(db.dup_readiness())

    @app.context_processor
    def inject_helpers():
        labels = {
            config.STATE_PENDING: "排队中",
            config.STATE_RUNNING: "处理中",
            config.STATE_CANCELLING: "取消中",
            config.STATE_COMPLETED: "已完成",
            config.STATE_FAILED: "已失败",
            config.STATE_CANCELLED: "已取消",
            config.STATE_INTERRUPTED: "已中断",
        }
        return {"state_label": lambda s: labels.get(s, s)}

    # --------------------------------------------------------------- errors
    @app.errorhandler(413)
    def too_large(_err):
        return (
            render_template(
                "error.html",
                message=f"文件超过 {config.MAX_CONTENT_LENGTH // (1024 * 1024)} MiB 上限，已拒绝",
            ),
            413,
        )

    @app.errorhandler(404)
    def not_found(err):
        return render_template("error.html", message=err.description), 404

    @app.errorhandler(403)
    def forbidden(err):
        return render_template("error.html", message=err.description), 403

    return app


def _public(task):
    """Shape exposed to the browser polling endpoints."""
    return {
        "task_id": task["task_id"],
        "state": task["state"],
        "progress": task["progress"],
        "total": task["total"],
        "original_name": task["original_name"],
        "error": task["error"],
        "cancel_requested": bool(task["cancel_requested"]),
        "downloadable": task["state"] == config.STATE_COMPLETED,
        "created_at": task["created_at"],
        "finished_at": task["finished_at"],
    }
