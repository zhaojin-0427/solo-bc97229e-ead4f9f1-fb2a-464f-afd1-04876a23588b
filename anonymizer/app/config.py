"""Runtime configuration.

All paths live under DATA_DIR (default /data) so a single mounted volume
persists completed work across restarts and is shared between the web and
worker containers.
"""
import os

DATA_DIR = os.environ.get("ANON_DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "anonymizer.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
OUTPUT_DIR = os.path.join(DATA_DIR, "outputs")
TMP_DIR = os.path.join(DATA_DIR, "tmp")

# Upload ceiling: 5 MiB (binary mebibyte, per the specification).
MAX_CONTENT_LENGTH = 5 * 1024 * 1024

# Hard wall-clock limit for one rule applied to one record. The regex actually
# runs in a separate process; when this expires the process is killed, so the
# replacement genuinely stops rather than merely raising an error afterwards.
RULE_TIMEOUT_SECONDS = 2.0

# Worker polling cadence (seconds).
WORKER_POLL_INTERVAL = 0.5

# Duplicate-corpus check page: groups per page / sources per page.
DUP_GROUP_PAGE_SIZE = 20
DUP_SOURCE_PAGE_SIZE = 50

# Pause between two backfill batches so the historical index build never
# starves live anonymization work (seconds).
DUP_BACKFILL_INTERVAL = 0.1

# Terminal task states.
STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_CANCELLING = "cancelling"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_INTERRUPTED = "interrupted"

TERMINAL_STATES = (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_CANCELLED,
    STATE_INTERRUPTED,
)
