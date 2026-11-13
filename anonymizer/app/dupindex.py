"""Duplicate-corpus index: parse copies, backfill historical batches.

The index itself lives in SQLite next to the task table (see db.py), so
the check page answers from stored rows and never rescans copy files.
Copies are only read, never modified.

Lifecycle:
  * New completions: the worker parses the published copy and commits the
    batch's whole index in the SAME transaction as the success state
    (db.mark_completed).
  * Historical completions: on first startup a background thread indexes
    every already-completed batch one at a time (backfill_loop), while
    anonymization keeps running. Each batch commits independently, so a
    restart resumes at the first unindexed batch without double-counting.
  * Read failures are stored per batch with their reason; the page shows
    them and offers retry. Readiness is computed from the tables, so an
    incomplete index can never be reported as ready.
"""
import json
import time

from . import config, db


def build_occurrences(output_path):
    """Parse one completed copy into (line_no, session_id, text) triples.

    Only non-empty texts are returned: empty texts never form duplicate
    groups. This is pure file IO with no database transaction held, so
    reading a copy never occupies the task store's write lock. Memory is
    bounded by one batch (uploads are capped at 5 MiB), not by the total
    record count.
    """
    occurrences = []
    with open(output_path, "r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            text = obj.get("text")
            if not isinstance(text, str) or text == "":
                continue
            session_id = obj.get("session_id", obj.get("id"))
            if not isinstance(session_id, str):
                session_id = ""
            occurrences.append((line_no, session_id, text))
    return occurrences


def index_batch(task_id, output_path):
    """Index one completed batch. Returns (ok, error).

    The copy is read BEFORE any write transaction starts; the insert
    itself is idempotent, so retries and restarts never double-count. A
    read failure is recorded with its reason (and never downgrades an
    already-indexed batch).
    """
    try:
        occurrences = build_occurrences(output_path)
    except (OSError, ValueError, TypeError) as exc:
        error = f"读取副本失败：{exc}"
        db.mark_batch_failed(task_id, error)
        return False, error
    db.store_batch_index(task_id, occurrences)
    return True, None


def backfill_loop(stop_event):
    """Index every historical completed batch, one batch per transaction.

    Runs in a worker thread while anonymization continues. Batches that
    complete during the backfill index themselves at commit time, and
    readiness is computed over all completed batches, so the switch to
    ready automatically includes them. The loop exits once no completed
    batch is left unindexed; failed batches stay visible for manual retry.
    Unexpected errors are logged and retried — the thread must never die
    silently and leave the index looking unfinished without a reason.
    """
    while not stop_event.is_set():
        try:
            row = db.pick_unindexed_completed()
            if row is None:
                return
            index_batch(row["task_id"], row["output_path"])
        except Exception as exc:  # noqa: BLE001 - keep the backfill alive
            print(f"[dupindex] 补建异常（稍后继续）：{exc!r}", flush=True)
            time.sleep(1.0)
            continue
        time.sleep(config.DUP_BACKFILL_INTERVAL)
