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

            -- Duplicate-corpus index. Built once per completed batch and
            -- stored here, so the check page never rescans copy files.
            -- Distinct non-empty texts, deduplicated across batches.
            CREATE TABLE IF NOT EXISTS dup_texts (
                text_id INTEGER PRIMARY KEY,
                text    TEXT NOT NULL UNIQUE
            );
            -- One row per occurrence of a text in a completed copy.
            -- Duplicates inside the same batch are separate rows, so they
            -- are counted separately by construction.
            CREATE TABLE IF NOT EXISTS dup_occurrences (
                text_id    INTEGER NOT NULL REFERENCES dup_texts(text_id),
                task_id    TEXT NOT NULL,
                line_no    INTEGER NOT NULL,
                session_id TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_dup_occ_text
                ON dup_occurrences(text_id);
            CREATE INDEX IF NOT EXISTS idx_dup_occ_task
                ON dup_occurrences(task_id);
            -- Per-batch index bookkeeping: exactly one row per attempted
            -- batch. 'indexed' batches carry their full occurrence set in
            -- the same transaction; 'failed' rows keep the read error for
            -- display and retry. Readiness is computed from these tables.
            CREATE TABLE IF NOT EXISTS dup_index_batches (
                task_id    TEXT PRIMARY KEY,
                state      TEXT NOT NULL,
                error      TEXT,
                indexed_at TEXT NOT NULL
            );
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


def mark_completed(task_id, output_path, occurrences):
    """Publish a finished task together with its duplicate-corpus index.

    The success state and the batch's *entire* index commit in ONE
    transaction: a completed batch always has its full index, and a batch
    that is not completed never has index rows. ``occurrences`` is the
    parsed content of the published copy, collected before this
    transaction started (file IO never happens under the write lock).

    Returns True only if this call wins the race against cancellation; the
    cancel endpoint is serialized against this same update, so either the
    task publishes first (and the subsequent cancel is refused) or the cancel
    lands first (and the result plus its index are discarded).
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE;")
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
        if cur.rowcount != 1:
            conn.execute("ROLLBACK;")
            return False
        try:
            _store_batch_index(conn, task_id, occurrences)
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        conn.execute("COMMIT;")
        return True
    finally:
        conn.close()


# ------------------------------------------------------- duplicate index

def _store_batch_index(conn, task_id, occurrences):
    """Replace one batch's duplicate-index rows. Runs inside a transaction.

    Idempotent: old occurrences of the batch are removed first, so retries
    and restarts never double-count. Empty texts are skipped here as well
    as at collection time — they never form duplicate groups. Memory use
    is bounded by one batch (the caller parsed a single copy), never by
    the total record count.
    """
    conn.execute("DELETE FROM dup_occurrences WHERE task_id = ?", (task_id,))
    text_ids = {}
    for line_no, session_id, text in occurrences:
        if not text:
            continue
        text_id = text_ids.get(text)
        if text_id is None:
            row = conn.execute(
                "SELECT text_id FROM dup_texts WHERE text = ?", (text,)
            ).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO dup_texts (text) VALUES (?)", (text,)
                )
                text_id = cur.lastrowid
            else:
                text_id = row["text_id"]
            text_ids[text] = text_id
        conn.execute(
            """
            INSERT INTO dup_occurrences (text_id, task_id, line_no, session_id)
            VALUES (?, ?, ?, ?)
            """,
            (text_id, task_id, line_no, session_id),
        )
    conn.execute(
        """
        INSERT OR REPLACE INTO dup_index_batches
            (task_id, state, error, indexed_at)
        VALUES (?, 'indexed', NULL, ?)
        """,
        (task_id, utcnow_iso()),
    )


def store_batch_index(task_id, occurrences):
    """Commit one batch's index in its own transaction (backfill / retry).

    The in-transaction guard makes concurrent backfill and retry safe:
    an already-indexed batch is left untouched. Returns True if rows were
    (re)written, False when the batch was already indexed.
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE;")
        row = conn.execute(
            "SELECT state FROM dup_index_batches WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is not None and row["state"] == "indexed":
            conn.execute("ROLLBACK;")
            return False
        try:
            _store_batch_index(conn, task_id, occurrences)
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        conn.execute("COMMIT;")
        return True
    finally:
        conn.close()


def mark_batch_failed(task_id, error):
    """Record that a batch's copy could not be read for indexing.

    Never downgrades an already-indexed batch: a transient read error on
    an indexed batch must not make a healthy index look unfinished.
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE;")
        row = conn.execute(
            "SELECT state FROM dup_index_batches WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None or row["state"] != "indexed":
            conn.execute(
                """
                INSERT OR REPLACE INTO dup_index_batches
                    (task_id, state, error, indexed_at)
                VALUES (?, 'failed', ?, ?)
                """,
                (task_id, str(error), utcnow_iso()),
            )
        conn.execute("COMMIT;")
    finally:
        conn.close()


def pick_unindexed_completed():
    """Oldest completed batch with no index row yet (for the backfill)."""
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT t.task_id, t.output_path
              FROM tasks t
              LEFT JOIN dup_index_batches b ON b.task_id = t.task_id
             WHERE t.state = ? AND b.task_id IS NULL
             ORDER BY t.created_at
             LIMIT 1
            """,
            (config.STATE_COMPLETED,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def dup_readiness():
    """Computed readiness of the duplicate-corpus index.

    Ready means: every completed batch has an 'indexed' row — including
    batches that completed while the backfill was running (they commit
    their index together with their state). Any missing or failed batch
    keeps the index not-ready, so the check page can never silently
    return partial results.
    """
    conn = get_conn()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE state = ?",
            (config.STATE_COMPLETED,),
        ).fetchone()[0]
        indexed = conn.execute(
            "SELECT COUNT(*) FROM dup_index_batches WHERE state = 'indexed'"
        ).fetchone()[0]
        missing = conn.execute(
            """
            SELECT COUNT(*)
              FROM tasks t
              LEFT JOIN dup_index_batches b ON b.task_id = t.task_id
             WHERE t.state = ? AND b.task_id IS NULL
            """,
            (config.STATE_COMPLETED,),
        ).fetchone()[0]
        failed = [
            dict(r)
            for r in conn.execute(
                """
                SELECT b.task_id, b.error, b.indexed_at, t.original_name
                  FROM dup_index_batches b
                  JOIN tasks t ON t.task_id = b.task_id
                 WHERE b.state = 'failed'
                 ORDER BY b.indexed_at
                """
            )
        ]
    finally:
        conn.close()
    return {
        "ready": missing == 0 and not failed,
        "total": total,
        "indexed": indexed,
        "failed": failed,
    }


def dup_group_count():
    """Number of duplicate groups (texts occurring at least twice)."""
    conn = get_conn()
    try:
        return conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT o.text_id
                  FROM dup_occurrences o
                 GROUP BY o.text_id
                HAVING COUNT(*) >= 2
            )
            """
        ).fetchone()[0]
    finally:
        conn.close()


def dup_groups(page, size):
    """One page of duplicate groups, most frequent first.

    Grouping and counting run in SQL with LIMIT/OFFSET, so memory stays
    bounded by the page size no matter how many records exist in total.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT o.text_id AS text_id, t.text AS text,
                   COUNT(*) AS occurrences,
                   COUNT(DISTINCT o.task_id) AS batches
              FROM dup_occurrences o
              JOIN dup_texts t ON t.text_id = o.text_id
             GROUP BY o.text_id
            HAVING COUNT(*) >= 2
             ORDER BY occurrences DESC, o.text_id
             LIMIT ? OFFSET ?
            """,
            (size, (page - 1) * size),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def dup_group(text_id):
    """One group's text and total occurrence count, or None."""
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT t.text_id AS text_id, t.text AS text,
                   (SELECT COUNT(*) FROM dup_occurrences o
                     WHERE o.text_id = t.text_id) AS occurrences
              FROM dup_texts t
             WHERE t.text_id = ?
            """,
            (text_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def dup_group_sources(text_id, page, size):
    """One page of a group's sources (session, task, data line number)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT o.task_id AS task_id, o.line_no AS line_no,
                   o.session_id AS session_id,
                   t.original_name AS original_name,
                   t.created_at AS created_at
              FROM dup_occurrences o
              JOIN tasks t ON t.task_id = o.task_id
             WHERE o.text_id = ?
             ORDER BY t.created_at, o.task_id, o.line_no
             LIMIT ? OFFSET ?
            """,
            (text_id, size, (page - 1) * size),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


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
