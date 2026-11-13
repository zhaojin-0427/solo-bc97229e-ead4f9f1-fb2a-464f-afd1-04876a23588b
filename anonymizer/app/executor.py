"""Regex execution with an enforced per-rule, per-record wall-clock limit.

A plain signal-based timeout (``signal.alarm``) cannot interrupt a catastrophic
backtracking regex: ``re`` is C code running with the GIL held, so the Python
handler only runs *after* the match eventually returns. To make the 2-second
limit real, every substitution runs in a dedicated child process. When the
deadline passes the child is killed (SIGKILL) and replaced, so the actual
replacement stops immediately instead of burning CPU in the background.

The child caches compiled patterns, so within one task each pattern is
compiled once. No rule is rejected at the web layer for being "expensive":
any syntactically valid pattern is accepted, and the timeout (rather than an
allowlist) is what bounds execution.
"""
import multiprocessing as mp
import queue as queue_mod
import re


class RegexError(Exception):
    """The pattern failed to compile (a real replacement error)."""


class RegexTimeout(Exception):
    """The rule exceeded its deadline; the child has been terminated."""


class ExecutorDied(Exception):
    """The worker subprocess terminated unexpectedly."""


def _regex_worker(request_q, response_q):
    """Long-lived child process holding the compiled-pattern cache."""
    cache = {}

    def compiled(pattern):
        if pattern not in cache:
            cache[pattern] = re.compile(pattern)
        return cache[pattern]

    while True:
        req = request_q.get()
        if req is None:
            return
        seq, pattern, replacement, text = req
        try:
            result = compiled(pattern).sub(replacement, text)
        except re.error as exc:
            response_q.put((seq, "compile_error", str(exc)))
        except Exception as exc:  # pragma: no cover - defensive
            response_q.put((seq, "error", repr(exc)))
        else:
            response_q.put((seq, "ok", result))


class RegexExecutor:
    def __init__(self, timeout):
        # A bounded queue makes a wedged worker apply backpressure instead of
        # letting the parent queue unbounded work into memory.
        self._request_q = mp.Queue(maxsize=1)
        self._response_q = mp.Queue(maxsize=1)
        self._proc = mp.Process(
            target=_regex_worker,
            args=(self._request_q, self._response_q),
            daemon=True,
        )
        self._proc.start()
        self._timeout = timeout
        self._seq = 0

    def _spawn(self):
        if self._proc.is_alive():
            return
        old = self._proc
        old.join(timeout=0.1)
        self._request_q = mp.Queue(maxsize=1)
        self._response_q = mp.Queue(maxsize=1)
        self._proc = mp.Process(
            target=_regex_worker,
            args=(self._request_q, self._response_q),
            daemon=True,
        )
        self._proc.start()

    def substitute(self, pattern, replacement, text):
        """Run one ``re.sub`` with the enforced timeout."""
        if not self._proc.is_alive():
            raise ExecutorDied("regex worker process is not running")

        self._seq += 1
        seq = self._seq
        self._request_q.put((seq, pattern, replacement, text))

        try:
            answer = self._response_q.get(
                timeout=self._timeout
            )
        except queue_mod.Empty:
            # The match is still running. Terminate the actual computation,
            # wait for the process to disappear, and bring up a fresh one so
            # subsequent records can still be processed.
            self._proc.terminate()
            self._proc.join(timeout=5)
            if self._proc.is_alive():  # pragma: no cover
                self._proc.kill()
                self._proc.join(timeout=5)
            # Drain anything the dead process might have queued.
            self._drain(self._request_q)
            self._drain(self._response_q)
            self._spawn()
            raise RegexTimeout(
                f"规则 {pattern!r} 在单条记录上超过 {self._timeout:g} 秒"
            )

        r_seq, status, payload = answer
        if r_seq != seq or not self._proc.is_alive():
            raise ExecutorDied("regex worker returned an out-of-order result")
        if status == "ok":
            return payload
        if status == "compile_error":
            raise RegexError(f"正则编译失败 {pattern!r}: {payload}")
        raise ExecutorDied(f"regex worker 错误: {payload}")

    @staticmethod
    def _drain(q):
        while True:
            try:
                q.get_nowait()
            except queue_mod.Empty:
                return

    def close(self):
        if self._proc.is_alive():
            self._request_q.put(None)
            self._proc.join(timeout=5)
        if self._proc.is_alive():  # pragma: no cover
            self._proc.terminate()
            self._proc.join(timeout=5)
        if self._proc.is_alive():  # pragma: no cover
            self._proc.kill()
            self._proc.join()
        self._request_q.close()
        self._response_q.close()
        self._request_q.join_thread()
        self._response_q.join_thread()
