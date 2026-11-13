"""Background worker: validates, applies rules, publishes or fails atomically.

Only one task runs at a time in this worker, and it runs in its own process
group separate from the web service, so long regex work and cancellations
never block any page or any other request.
"""
import json
import multiprocessing as mp
import os
import signal
import sys
import time

from . import config, db
from .executor import ExecutorDied, RegexError, RegexExecutor, RegexTimeout
from .validation import ValidationError, read_ndjson


class BatchFailure(Exception):
    """Any condition that must fail the whole batch."""


def _tmp_output_path(task_id):
    return os.path.join(config.TMP_DIR, f"{task_id}.ndjson.tmp")


def _final_output_path(task_id):
    return os.path.join(config.OUTPUT_DIR, f"{task_id}.ndjson")


def _remove_quietly(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _check_cancel_requested(task_id):
    """Cheap read; the authoritative cancel/publish race lives in the DB."""
    task = db.get_task(task_id)
    return bool(task and task["cancel_requested"])


def process_task(task):
    task_id = task["task_id"]
    rules = json.loads(task["rules_json"])
    tmp_path = _tmp_output_path(task_id)
    executor = RegexExecutor(config.RULE_TIMEOUT_SECONDS)

    try:
        # ---- Phase 1: whole-batch validation (no output file yet) ---------
        try:
            records = read_ndjson(task["input_path"])
        except ValidationError as exc:
            raise BatchFailure(str(exc)) from exc

        total = len(records)
        db.set_total(task_id, total)

        # Compile every pattern once up front. A genuine syntax error fails
        # the batch before a single output line is written.
        for pattern, _replacement in rules:
            try:
                executor.substitute(pattern, "", "")
            except RegexError as exc:
                raise BatchFailure(str(exc)) from exc
            except (RegexTimeout, ExecutorDied):
                # A trivial empty-string probe cannot legitimately time out;
                # treat it as an infrastructure failure of the whole batch.
                raise BatchFailure("正则执行进程异常，整批失败")

        if _check_cancel_requested(task_id):
            db.mark_cancelled(task_id)
            return

        # ---- Phase 2: sequential replacement, temp file only --------------
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as out:
            for index, (_line_no, obj, _original) in enumerate(records, start=1):
                if _check_cancel_requested(task_id):
                    out.flush()
                    os.fsync(out.fileno())
                    out.close()
                    _remove_quietly(tmp_path)
                    db.mark_cancelled(task_id)
                    return

                text = obj["text"]
                try:
                    for pattern, replacement in rules:
                        text = executor.substitute(pattern, replacement, text)
                except RegexTimeout as exc:
                    raise BatchFailure(
                        f"第 {index} 条记录触发规则超时，整批失败：{exc}"
                    ) from exc
                except RegexError as exc:
                    raise BatchFailure(
                        f"第 {index} 条记录替换出错，整批失败：{exc}"
                    ) from exc
                except ExecutorDied as exc:
                    raise BatchFailure(
                        f"正则执行进程异常终止，整批失败：{exc}"
                    ) from exc

                obj["text"] = text
                # ensure_ascii=False keeps Chinese readable; \n line endings
                # make the copy portable. Field order and extras are kept.
                out.write(
                    json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
                db.update_progress(task_id, index)

            out.flush()
            os.fsync(out.fileno())

        # ---- Phase 3: publish, or lose the race to a cancellation ---------
        final_path = _final_output_path(task_id)
        os.replace(tmp_path, final_path)

        if not db.mark_completed(task_id, final_path):
            # Cancellation was accepted first. The result must never appear:
            # remove the published file and settle the task as cancelled.
            _remove_quietly(final_path)
            db.mark_cancelled(task_id)
            return

    except BatchFailure:
        raise
    except Exception as exc:  # noqa: BLE001 - any surprise fails the batch
        raise BatchFailure(f"未预期的处理错误，整批失败：{exc!r}") from exc
    finally:
        executor.close()
        _remove_quietly(tmp_path)


_shutdown = {"flag": False}


def _handle_signal(signum, _frame):
    _shutdown["flag"] = True


def main():
    # The regex executor forks a short-lived child per running task. Fork is
    # safe because the worker holds no threads or held locks when the child is
    # created; pin it explicitly so container environments never silently pick
    # a different start method.
    try:
        mp.set_start_method("fork")
    except RuntimeError:
        pass  # already configured (e.g. re-entrant in tests)

    db.init_storage()
    # Recovery runs only here (the web container never touches task states):
    # in-flight work from a previous worker becomes interrupted, queued
    # cancellations are settled, and partial temp files are removed.
    db.mark_unfinished_interrupted()
    db.cleanup_orphan_tmp_files()
    db.settle_cancelled_tasks()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    print("[worker] started, waiting for tasks", flush=True)
    while not _shutdown["flag"]:
        db.settle_cancelled_tasks()
        task = db.claim_next_task()
        if task is None:
            time.sleep(config.WORKER_POLL_INTERVAL)
            continue

        print(f"[worker] picked task {task['task_id']}", flush=True)
        tmp_path = _tmp_output_path(task["task_id"])
        try:
            process_task(task)
            print(f"[worker] task {task['task_id']} finished", flush=True)
        except BatchFailure as exc:
            _remove_quietly(tmp_path)
            db.mark_failed(task["task_id"], str(exc))
            print(
                f"[worker] task {task['task_id']} failed: {exc}", flush=True
            )
        except Exception as exc:  # noqa: BLE001
            _remove_quietly(tmp_path)
            db.mark_failed(task["task_id"], f"worker 内部错误：{exc!r}")
            print(f"[worker] task {task['task_id']} crashed: {exc!r}", flush=True)

    # On graceful shutdown, leave the running task for startup recovery to
    # mark as interrupted. Nothing here is downloadable until completion.
    print("[worker] shutting down", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
