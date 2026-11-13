#!/usr/bin/env bash
# Drive the running web service (http://localhost:8000) end to end with curl.
# Requires: docker compose up --build (or a local server on $BASE_URL).
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "== 1. 首页可访问 =="
curl -fsS "$BASE_URL/" -o /dev/null && echo "GET / -> 200"

echo "== 2. 上传正常样例 =="
TASK_PAGE=$(curl -fsS -i -X POST "$BASE_URL/tasks" \
  -F "file=@$HERE/../demo/sample.ndjson;type=application/x-ndjson" \
  -F "rules=<$HERE/../demo/rules.json;type=text/plain" | grep -i '^location:' | tr -d '\r' | awk '{print $2}')
echo "任务详情页: $TASK_PAGE"
TASK_ID="${TASK_PAGE##*/}"

echo "== 3. 轮询进度直到终态 =="
for _ in $(seq 1 60); do
  STATE=$(curl -fsS "$BASE_URL/api/tasks/$TASK_ID" | python3 -c 'import sys,json;print(json.load(sys.stdin)["task"]["state"])')
  echo "  state=$STATE"
  case "$STATE" in completed|failed|cancelled|interrupted) break;; esac
  sleep 1
done

echo "== 4. 下载完整脱敏副本 =="
curl -fsS "$BASE_URL/tasks/$TASK_ID/download" -o /tmp/anonymized.ndjson
echo "--- 脱敏结果预览 ---"
cat /tmp/anonymized.ndjson

echo
echo "== 5. 超时规则整批失败演示 =="
SLOW_PAGE=$(curl -fsS -i -X POST "$BASE_URL/tasks" \
  -F "file=@$HERE/../demo/sample-timeout.ndjson;type=application/x-ndjson" \
  -F "rules=<$HERE/../demo/rules-timeout.json;type=text/plain" | grep -i '^location:' | tr -d '\r' | awk '{print $2}')
SLOW_ID="${SLOW_PAGE##*/}"
for _ in $(seq 1 30); do
  JSON=$(curl -fsS "$BASE_URL/api/tasks/$SLOW_ID")
  STATE=$(echo "$JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin)["task"]["state"])')
  echo "  state=$STATE"
  [ "$STATE" = "failed" ] && { echo "$JSON" | python3 -m json.tool; break; }
  sleep 1
done
echo "失败任务下载应被拒绝（HTTP 403）:"
curl -s -o /dev/null -w "  HTTP %{http_code}\n" "$BASE_URL/tasks/$SLOW_ID/download"
