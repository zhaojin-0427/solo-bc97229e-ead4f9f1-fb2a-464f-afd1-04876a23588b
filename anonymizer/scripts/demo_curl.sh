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

echo
echo "== 6. 重复语料核查：再传一次同一文件制造跨批重复 =="
TASK2_PAGE=$(curl -fsS -i -X POST "$BASE_URL/tasks" \
  -F "file=@$HERE/../demo/sample.ndjson;type=application/x-ndjson" \
  -F "rules=<$HERE/../demo/rules.json;type=text/plain" | grep -i '^location:' | tr -d '\r' | awk '{print $2}')
TASK2_ID="${TASK2_PAGE##*/}"
for _ in $(seq 1 60); do
  STATE=$(curl -fsS "$BASE_URL/api/tasks/$TASK2_ID" | python3 -c 'import sys,json;print(json.load(sys.stdin)["task"]["state"])')
  echo "  state=$STATE"
  case "$STATE" in completed|failed|cancelled|interrupted) break;; esac
  sleep 1
done

echo "== 7. 重复语料索引状态（ready 后核查页才有分组数据）=="
curl -fsS "$BASE_URL/api/duplicates/status" | python3 -m json.tool

echo "== 8. 核查页应列出样例中的重复行（同批两次 + 跨批各一次，共 4 次）=="
curl -fsS "$BASE_URL/duplicates" -o /tmp/duplicates.html
if grep -q "重复行测试" /tmp/duplicates.html && grep -q "查看来源" /tmp/duplicates.html; then
  echo "  OK: 核查页已列出重复分组（含同批重复分别计数）"
else
  echo "  注意: 未在核查页找到重复分组，请检查索引状态" >&2
  exit 1
fi
echo "浏览器打开 $BASE_URL/duplicates 查看完整核查页与每组来源"
