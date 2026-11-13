#!/bin/sh
# Single image, two roles selected by ROLE:
#   web    -> gunicorn serving the Flask app (multiple sync workers)
#   worker -> one long-running background process (own process group)
set -e

case "${ROLE:-web}" in
  web)
    echo "[entrypoint] starting web (gunicorn)"
    exec gunicorn \
      --bind 0.0.0.0:8000 \
      --workers "${GUNICORN_WORKERS:-2}" \
      --threads "${GUNICORN_THREADS:-2}" \
      --timeout 60 \
      --access-logfile - \
      --error-logfile - \
      "app.server.app:create_app()"
    ;;
  worker)
    echo "[entrypoint] starting worker"
    exec python -m app.worker
    ;;
  *)
    echo "unknown ROLE=$ROLE (expected web|worker)" >&2
    exit 1
    ;;
esac
