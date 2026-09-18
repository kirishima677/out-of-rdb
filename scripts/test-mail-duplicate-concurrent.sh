#!/bin/sh
# Verify two near-simultaneous Lambda invocations store every duplicate record.
# ほぼ同時の Lambda 2 回実行で、重複レコードがすべて保存されることを検証する。

set -eu

LOCALSTACK_CONTAINER="${LOCALSTACK_CONTAINER:-localstack}"
RECORD_COUNT="${RECORD_COUNT:-100}"
DELAY_SECONDS="${DELAY_SECONDS:-0.5}"
KEEP_TEST_DATA="${KEEP_TEST_DATA:-0}"
BUCKET="mail-send-logs"
# Match the detector's table name so tests target the same environment.
# 検知側のテーブル名に合わせ、テストが同じ環境を対象にするようにする。
TABLE="$(docker exec "${LOCALSTACK_CONTAINER}" awslocal lambda get-function \
  --function-name detect-mail-duplicates \
  --query 'Configuration.Environment.Variables.TABLE_NAME' \
  --output text 2>/dev/null || echo '')"
if [ -z "${TABLE}" ] || [ "${TABLE}" = "None" ]; then
  TABLE="mail_send_log_events"
fi
# Numeric IDs satisfy the current detector pattern and are unique per test run.
# 数値 ID は現在の検知パターンに合い、テスト実行ごとに一意となる。
RUN_ID="$(date +%s)$$"
# Match the detector's prefix filter so test objects are not skipped.
# 検知側のプレフィックスフィルタに合わせ、テスト用オブジェクトが無視されないようにする。
DETECTOR_KEY_PREFIX="$(docker exec "${LOCALSTACK_CONTAINER}" awslocal lambda get-function \
  --function-name detect-mail-duplicates \
  --query 'Configuration.Environment.Variables.TARGET_KEY_PREFIX' \
  --output text 2>/dev/null || echo '')"
if [ "${DETECTOR_KEY_PREFIX}" = "None" ]; then
  DETECTOR_KEY_PREFIX=""
fi
KEY_PREFIX="${DETECTOR_KEY_PREFIX}concurrent/${RUN_ID}"
FIRST_KEY="${KEY_PREFIX}/mail-send-1.log"
SECOND_KEY="${KEY_PREFIX}/mail-send-2.log"
SLACK_WEBHOOK_URL="$(docker exec "${LOCALSTACK_CONTAINER}" awslocal lambda get-function \
  --function-name detect-mail-duplicates \
  --query 'Configuration.Environment.Variables.SLACK_WEBHOOK_URL' \
  --output text)"
MOCK_SLACK_URL="$(printf '%s' "${SLACK_WEBHOOK_URL}" | sed 's#://localstack:4566/#://localhost:4566/#')"

case "${RECORD_COUNT}" in
  ''|*[!0-9]*|0)
    echo "RECORD_COUNT must be a positive integer.（正の整数を指定してください）" >&2
    exit 2
    ;;
esac

cleanup() {
  if [ "${KEEP_TEST_DATA}" = "1" ]; then
    # Preserve records only when explicitly requested for manual inspection.
    # 手動確認を明示的に指定した場合だけ、レコードを保持する。
    return
  fi

  # Delete only records whose source key belongs to this run.
  # この実行の sourceKey を持つレコードだけを削除する。
  docker exec "${LOCALSTACK_CONTAINER}" awslocal s3 rm "s3://${BUCKET}/${FIRST_KEY}" >/dev/null 2>&1 || true
  docker exec "${LOCALSTACK_CONTAINER}" awslocal s3 rm "s3://${BUCKET}/${SECOND_KEY}" >/dev/null 2>&1 || true
  docker exec -i -e TABLE_NAME="${TABLE}" "${LOCALSTACK_CONTAINER}" python3 - "${KEY_PREFIX}/" <<'PY' >/dev/null 2>&1 || true
import os
import sys

import boto3
from boto3.dynamodb.conditions import Attr

table = boto3.resource(
    "dynamodb",
    endpoint_url="http://dynamodb:8000",
    region_name="us-east-1",
    aws_access_key_id="local",
    aws_secret_access_key="local",
).Table(os.environ["TABLE_NAME"])

response = table.scan(FilterExpression=Attr("sourceKey").begins_with(sys.argv[1]))
items = response["Items"]
while "LastEvaluatedKey" in response:
    response = table.scan(
        FilterExpression=Attr("sourceKey").begins_with(sys.argv[1]),
        ExclusiveStartKey=response["LastEvaluatedKey"],
    )
    items.extend(response["Items"])

with table.batch_writer() as batch:
    for item in items:
        batch.delete_item(Key={"EmailID": item["EmailID"], "recordKey": item["recordKey"]})
PY
  # Remove mock delivery counters created by this test as well.
  # このテストで作られた疑似 Slack の配送カウンターも削除する。
  curl -fsS -o /dev/null -X POST "${MOCK_SLACK_URL}?reset=true" || true
}

wait_for_and_verify_records() {
  attempt=1
  while [ "${attempt}" -le 30 ]; do
    if docker exec -i -e TABLE_NAME="${TABLE}" "${LOCALSTACK_CONTAINER}" python3 - "${KEY_PREFIX}/" "${RECORD_COUNT}" <<'PY'
import os
import sys
from collections import Counter

import boto3
from boto3.dynamodb.conditions import Attr

source_prefix, record_count = sys.argv[1], int(sys.argv[2])
table = boto3.resource(
    "dynamodb",
    endpoint_url="http://dynamodb:8000",
    region_name="us-east-1",
    aws_access_key_id="local",
    aws_secret_access_key="local",
).Table(os.environ["TABLE_NAME"])

response = table.scan(FilterExpression=Attr("sourceKey").begins_with(source_prefix))
items = response["Items"]
while "LastEvaluatedKey" in response:
    response = table.scan(
        FilterExpression=Attr("sourceKey").begins_with(source_prefix),
        ExclusiveStartKey=response["LastEvaluatedKey"],
    )
    items.extend(response["Items"])

counts = Counter(item["EmailID"] for item in items)
expected_total = record_count * 2
if len(items) == expected_total and len(counts) == record_count and all(
    count == 2 for count in counts.values()
):
    print(f"verified {len(items)} records: {record_count} EmailIDs x 2")
    raise SystemExit(0)

print(
    f"waiting: {len(items)}/{expected_total} records, "
    f"{len(counts)}/{record_count} distinct EmailIDs"
)
raise SystemExit(1)
PY
    then
      return 0
    fi
    sleep 1
    attempt=$((attempt + 1))
  done

  echo "Timed out before all duplicate records were stored." >&2
  return 1
}

trap cleanup EXIT HUP INT TERM
curl -fsS -o /dev/null -X POST "${MOCK_SLACK_URL}?reset=true"

echo "Testing concurrent duplicate delivery: ${RECORD_COUNT} records x 2, ${DELAY_SECONDS}s apart"
docker exec -i "${LOCALSTACK_CONTAINER}" sh -s -- "${RUN_ID}" "${RECORD_COUNT}" <<'EOS'
set -eu
run_id="$1"
record_count="$2"

for file_number in 1 2; do
  file="/tmp/mail-duplicate-concurrent-${file_number}.log"
  : > "${file}"
  iso_timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  log_timestamp="$(date -u '+%Y-%m-%d %H:%M:%S')"
  index=1
  while [ "${index}" -le "${record_count}" ]; do
    email_id="${run_id}$(printf '%03d' "${index}")"
    printf '{"date":"%s","log":"[%s] production.INFO: SendEmails [production] success  sending email: %s"}\n' "$iso_timestamp" "$log_timestamp" "$email_id" >> "${file}"
    index=$((index + 1))
  done
done
EOS

# The uploads are intentionally not awaited by Lambda; the short delay lets their
# Lambda executions overlap on a typical local run.
# Lambda の完了を待たずに配送する。短い間隔により通常は実行時間が重なる。
docker exec "${LOCALSTACK_CONTAINER}" awslocal s3 cp /tmp/mail-duplicate-concurrent-1.log "s3://${BUCKET}/${FIRST_KEY}"
sleep "${DELAY_SECONDS}"
docker exec "${LOCALSTACK_CONTAINER}" awslocal s3 cp /tmp/mail-duplicate-concurrent-2.log "s3://${BUCKET}/${SECOND_KEY}"

wait_for_and_verify_records
echo "PASS: ${RECORD_COUNT} EmailIDs were each stored twice (${RECORD_COUNT} x 2 = $((RECORD_COUNT * 2)) records)."
if [ "${KEEP_TEST_DATA}" = "1" ]; then
  echo "Keeping records for inspection. Source prefix: ${KEY_PREFIX}/"
else
  echo "Cleaning up this test's S3 objects and DynamoDB records."
fi
