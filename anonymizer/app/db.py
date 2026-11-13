"""SQLite-backed task store.

SQLite's write lock serializes the state transitions that matter (claim,
cancel, complete). WAL mode plus a busy timeout keeps concurrent reads in the
web process responsive while the worker writes.
"""
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from . import config


def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def init_storage():
    os.makedirs(config.UPLOAD_DIR, exist_ok=True)
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    os.makedirs(config.TMP_DIR, exist_ok=True)
    conn = get_conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id        TEXT PRIMARY KEY,
                state          TEXT NOT NULL,
                progress       INTEGER NOT NULL DEFAULT 0,
                total          INTEGER NOT NULL DEFAULT 0,
                original_name  TEXT NOT NULL,
                input_path     TEXT NOT NULL,
                output_path    TEXT,
                rules_json     TEXT NOT NULL,
                error          TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at     TEXT NOT NULL,
                started_at     TEXT,
                finished_at    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
            """
        )
        conn.commit()
    finally:
        conn.close()


def get_conn():
    conn = sqlite3.connect(
        config.DB_PATH, timeout=30, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def create_task(original_name, input_path, rules, task_id=None):
    task_id = task_id or uuid.uuid4().hex
    now = utcnow_iso()
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO tasks (task_id, state, original_name, input_path,
                               rules_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                config.STATE_PENDING,
                original_name,
                input_path,
                json.dumps(rules, ensure_ascii=False),
                now,
            ),
        )
    finally:
        conn.close()
    return task_id


def get_task(task_id):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def list_tasks():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def claim_next_task():
    """Atomically take one pending task. Returns task dict or None.

    Tasks whose cancellation was requested while still pending are not
    claimed: the worker settles them as cancelled instead of doing any
    replacement work.
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE;")
        row = conn.execute(
            """
            SELECT * FROM tasks
             WHERE state = ? AND cancel_requested = 0
             ORDER BY created_at LIMIT 1
            """,
            (config.STATE_PENDING,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT;")
            return None
        conn.execute(
            "UPDATE tasks SET state = ?, started_at = ? WHERE task_id = ?",
            (config.STATE_RUNNING, utcnow_iso(), row["task_id"]),
        )
        conn.execute("COMMIT;")
        return dict(row)
    finally:
        conn.close()


def set_total(task_id, total):
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE tasks SET total = ? WHERE task_id = ?", (total, task_id)
        )
    finally:
        conn.close()


def update_progress(task_id, progress):
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE tasks SET progress = ? WHERE task_id = ?",
            (progress, task_id),
        )
    finally:
        conn.close()


def mark_completed(task_id, output_path):
    """Publish a finished task.

    Returns True only if this call wins the race against cancellation; the
    cancel endpoint is serialized against this same update, so either the
    task publishes first (and the subsequent cancel is refused) or the cancel
    lands first (and the result is discarded).
    """
    conn = get_conn()
    try:
        cur = conn.execute(
            """
            UPDATE tasks
               SET state = ?, output_path = ?, finished_at = ?
             WHERE task_id = ?
               AND cancel_requested = 0
               AND state IN (?, ?)
            """,
            (
                config.STATE_COMPLETED,
                output_path,
                utcnow_iso(),
                task_id,
                config.STATE_RUNNING,
                config.STATE_CANCELLING,
            ),
        )
        return cur.rowcount == 1
    finally:
        conn.close()


def mark_failed(task_id, error):
    conn = get_conn()
    try:
        conn.execute(
            """
            UPDATE tasks
               SET state = ?, error = ?, finished_at = ?
             WHERE task_id = ?
               AND state NOT IN (?, ?, ?, ?)
            """,
            (
                config.STATE_FAILED,
                str(error),
                utcnow_iso(),
                task_id,
                config.STATE_COMPLETED,
                config.STATE_CANCELLED,
                config.STATE_INTERRUPTED,
                config.STATE_FAILED,
            ),
        )
    finally:
        conn.close()


def request_cancel(task_id):
    """Accept a cancellation request.

    Accepts the request while the task is pending or in progress; refuses if
    the task has already reached a terminal state (in particular, a task that
    published first stays completed and downloadable).

    Returns (accepted: bool, current_state: str | None).
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE;")
        row = conn.execute(
            "SELECT state FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            conn.execute("COMMIT;")
            return False, None
        state = row["state"]
        if state in config.TERMINAL_STATES:
            conn.execute("COMMIT;")
            return False, state
        if state == config.STATE_RUNNING:
            # An in-flight task moves to cancelling so the processing loop,
            # which checks the flag between records, stops as soon as it can.
            conn.execute(
                """
                UPDATE tasks
                   SET cancel_requested = 1, state = ?
                 WHERE task_id = ?
                """,
                (config.STATE_CANCELLING, task_id),
            )
        else:
            # A still-queued task keeps the pending state with only the flag
            # set. It is never claimed (claim_next_task skips flagged rows)
            # and is settled as cancelled on the next worker sweep.
            conn.execute(
                """
                UPDATE tasks
                   SET cancel_requested = 1
                 WHERE task_id = ?
                """,
                (task_id,),
            )
        conn.execute("COMMIT;")
        return True, state
    finally:
        conn.close()


def mark_cancelled(task_id):
    conn = get_conn()
    try:
        conn.execute(
            """
            UPDATE tasks
               SET state = ?, finished_at = ?
             WHERE task_id = ? AND state = ?
            """,
            (
                config.STATE_CANCELLED,
                utcnow_iso(),
                task_id,
                config.STATE_CANCELLING,
            ),
        )
    finally:
        conn.close()


def settle_cancelled_tasks():
    """Settle accepted cancellations that never need replacement work.

    Two groups are finalized as cancelled:
      * pending rows flagged before any worker claimed them (no file exists),
      * cancelling rows the previous worker left behind on shutdown.
    In-flight cancellations are finalized inside ``process_task`` once the
    actual replacement work has stopped and remnants are cleaned.
    """
    conn = get_conn()
    try:
        conn.execute(
            """
            UPDATE tasks
               SET state = ?, finished_at = ?
             WHERE state = ?
                OR (state = ? AND cancel_requested = 1)
            """,
            (
                config.STATE_CANCELLED,
                utcnow_iso(),
                config.STATE_CANCELLING,
                config.STATE_PENDING,
            ),
        )
    finally:
        conn.close()


def mark_unfinished_interrupted():
    """Startup recovery.

    Only tasks that were actually in flight (running / cancelling) when the
    previous worker stopped are marked interrupted: such tasks may hold a
    partial temp file and can never produce a downloadable copy. Tasks still
    in ``pending`` were never picked up by any worker, so they stay pending
    and the new worker claims them normally. Completed copies are untouched.
    """
    conn = get_conn()
    try:
        conn.execute(
            """
            UPDATE tasks
               SET state = ?, error = COALESCE(error, ?), finished_at = ?
             WHERE state IN (?, ?)
            """,
            (
                config.STATE_INTERRUPTED,
                "任务因服务重启而中断",
                utcnow_iso(),
                config.STATE_RUNNING,
                config.STATE_CANCELLING,
            ),
        )
    finally:
        conn.close()


def cleanup_orphan_tmp_files():
    """Remove partial output remnants left behind by an interrupted run."""
    if not os.path.isdir(config.TMP_DIR):
        return
    for name in os.listdir(config.TMP_DIR):
        path = os.path.join(config.TMP_DIR, name)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
