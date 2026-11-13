"""Flask web entry point: upload, progress, cancel, download."""
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

from .. import config, db
from ..validation import ValidationError, load_rules


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
