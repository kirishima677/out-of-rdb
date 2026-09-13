#!/bin/sh
# Benchmark one S3 event containing many mail-send log lines.
# 多数のメール送信ログ行を含む 1 件の S3 イベントをベンチマークする。

set -eu

LOCALSTACK_CONTAINER="${LOCALSTACK_CONTAINER:-localstack}"
RECORD_COUNT="${RECORD_COUNT:-100}"
KEEP_TEST_DATA="${KEEP_TEST_DATA:-0}"
BUCKET="mail-send-logs"
TABLE="mail_duplicate_events"
# Create numeric IDs so they match the detector's current EmailID pattern.
# 現在の検知正規表現に合うよう、数値の EmailID を作る。
RUN_ID="$(date +%s)$$"
OBJECT_KEY="benchmark/${RUN_ID}/mail-send-${RECORD_COUNT}.log"

case "${RECORD_COUNT}" in
  ''|*[!0-9]*|0)
    echo "RECORD_COUNT must be a positive integer.（正の整数を指定してください）" >&2
    exit 2
    ;;
esac

cleanup() {
  if [ "${KEEP_TEST_DATA}" = "1" ]; then
    # Keep records only when explicitly requested for inspection.
    # 手動確認を明示的に指定した場合だけレコードを残す。
    return
  fi

  # Remove only this run's S3 object and its matching DynamoDB records.
  # この実行の S3 オブジェクトと対応する DynamoDB レコードだけを削除する。
  docker exec "${LOCALSTACK_CONTAINER}" awslocal s3 rm "s3://${BUCKET}/${OBJECT_KEY}" >/dev/null 2>&1 || true

  # Batch-delete by sourceKey to avoid one Docker process per record.
  # レコードごとに Docker プロセスを作らないよう、sourceKey 単位で一括削除する。
  docker exec -i "${LOCALSTACK_CONTAINER}" python3 - "${OBJECT_KEY}" <<'PY' >/dev/null 2>&1 || true
import sys

import boto3
from boto3.dynamodb.conditions import Attr

table = boto3.resource(
    "dynamodb",
    endpoint_url="http://dynamodb:8000",
    region_name="us-east-1",
    aws_access_key_id="local",
    aws_secret_access_key="local",
).Table("mail_duplicate_events")

response = table.scan(FilterExpression=Attr("sourceKey").eq(sys.argv[1]))
items = response["Items"]
while "LastEvaluatedKey" in response:
    response = table.scan(
        FilterExpression=Attr("sourceKey").eq(sys.argv[1]),
        ExclusiveStartKey=response["LastEvaluatedKey"],
    )
    items.extend(response["Items"])

with table.batch_writer() as batch:
    for item in items:
        batch.delete_item(Key={"EmailID": item["EmailID"], "createdAt": item["createdAt"]})
PY
}

wait_for_records() {
  attempt=1
  while [ "${attempt}" -le 30 ]; do
    count="$(docker exec "${LOCALSTACK_CONTAINER}" awslocal dynamodb scan \
      --endpoint-url http://dynamodb:8000 \
      --table-name "${TABLE}" \
      --filter-expression 'sourceKey = :source_key' \
      --expression-attribute-values "{\":source_key\":{\"S\":\"${OBJECT_KEY}\"}}" \
      --select COUNT \
      --query Count \
      --output text 2>/dev/null || true)"
    if [ "${count}" = "${RECORD_COUNT}" ]; then
      return 0
    fi
    sleep 1
    attempt=$((attempt + 1))
  done

  echo "Timed out waiting for ${RECORD_COUNT} record(s); current count: ${count:-0}" >&2
  return 1
}

trap cleanup EXIT HUP INT TERM

START_TIME_MS="$(python3 -c 'import time; print(int(time.time() * 1000))')"
START_TIME="$(python3 -c 'import time; print(time.perf_counter())')"

echo "Benchmarking ${RECORD_COUNT} records with S3 object: ${OBJECT_KEY}"
docker exec -i "${LOCALSTACK_CONTAINER}" sh -s -- "${RUN_ID}" "${RECORD_COUNT}" "${OBJECT_KEY}" <<'EOS'
set -eu
run_id="$1"
record_count="$2"
object_key="$3"

: > /tmp/mail-duplicate-benchmark.log
index=1
while [ "${index}" -le "${record_count}" ]; do
  email_id="${run_id}$(printf '%03d' "${index}")"
  printf '%s\n' "[2026-09-13 10:00:00] production.INFO: SendEmails [production] success  sending email: ${email_id}" >> /tmp/mail-duplicate-benchmark.log
  index=$((index + 1))
done
awslocal s3 cp /tmp/mail-duplicate-benchmark.log "s3://mail-send-logs/${object_key}"
EOS

wait_for_records
END_TIME="$(python3 -c 'import time; print(time.perf_counter())')"
END_TO_END_SECONDS="$(python3 -c "print(round(${END_TIME} - ${START_TIME}, 3))")"

# The detector handles one S3 object in one Lambda invocation, so its REPORT line
# represents the Lambda execution time for all generated records.
# 1つの S3 オブジェクトを1回の Lambda 実行で処理するため、REPORT 行が全件の実行時間となる。
REPORT_LINE="$(docker exec "${LOCALSTACK_CONTAINER}" awslocal logs filter-log-events \
  --log-group-name /aws/lambda/detect-mail-duplicates \
  --start-time "${START_TIME_MS}" \
  --output text 2>/dev/null | grep 'REPORT RequestId' | tail -n 1 || true)"
LAMBDA_DURATION_MS="$(printf '%s\n' "${REPORT_LINE}" | sed -n 's/.*Duration: \([0-9.]*\) ms.*/\1/p')"

echo "PASS: ${RECORD_COUNT} records were saved to DynamoDB Local."
echo "End-to-end time (S3 upload to all records saved): ${END_TO_END_SECONDS}s"
if [ -n "${LAMBDA_DURATION_MS}" ]; then
  echo "Lambda Duration (REPORT): ${LAMBDA_DURATION_MS} ms"
else
  echo "Lambda Duration (REPORT): unavailable"
fi

if [ "${KEEP_TEST_DATA}" = "1" ]; then
  echo "Keeping benchmark data for inspection. Source key: ${OBJECT_KEY}"
else
  echo "Cleaning up this benchmark's S3 object and DynamoDB records."
fi
