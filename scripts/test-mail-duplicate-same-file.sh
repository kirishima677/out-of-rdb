#!/bin/sh
# Verify two matching lines inside ONE S3 object are stored as two records.
# 1つの S3 オブジェクト内の2行が、2件のレコードとして保存されることを検証する。
#
# 同一オブジェクト内の行は同じ Lambda 実行で処理されるため、createdAt と sourceKey が
# 同値になる。SK が createdAt だけだと PK+SK が完全一致して2件目が1件目を上書きし、
# 件数が1のままとなって検知できない。SK に sourceLineNumber を含めることでこれを防ぐ。
# 本スクリプトはその回帰テストである。

set -eu

LOCALSTACK_CONTAINER="${LOCALSTACK_CONTAINER:-localstack}"
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
# Set to 1 to retain this run's records for manual inspection.
# 手動確認のため、この実行のレコードを残す場合は 1 を指定する。
KEEP_TEST_DATA="${KEEP_TEST_DATA:-0}"

RUN_ID="$(date +%s)$$"
# Numeric IDs match the detector's EmailID pattern.
# 数値 ID は検知側の EmailID パターンに合致する。
DUPLICATE_EMAIL_ID="77${RUN_ID}"
SINGLE_EMAIL_ID="88${RUN_ID}"

# Match the detector's prefix filter so test objects are not skipped.
# 検知側のプレフィックスフィルタに合わせ、テスト用オブジェクトが無視されないようにする。
DETECTOR_KEY_PREFIX="$(docker exec "${LOCALSTACK_CONTAINER}" awslocal lambda get-function \
  --function-name detect-mail-duplicates \
  --query 'Configuration.Environment.Variables.TARGET_KEY_PREFIX' \
  --output text 2>/dev/null || echo '')"
if [ "${DETECTOR_KEY_PREFIX}" = "None" ]; then
  DETECTOR_KEY_PREFIX=""
fi
OBJECT_KEY="${DETECTOR_KEY_PREFIX}same-file/${RUN_ID}/mail-send.log"

SLACK_WEBHOOK_URL="$(docker exec "${LOCALSTACK_CONTAINER}" awslocal lambda get-function \
  --function-name detect-mail-duplicates \
  --query 'Configuration.Environment.Variables.SLACK_WEBHOOK_URL' \
  --output text)"
# The Lambda uses the Compose hostname; this shell calls the published host port.
# Lambda は Compose のホスト名を使うため、このシェルでは公開済みホストポートへ変換する。
MOCK_SLACK_URL="$(printf '%s' "${SLACK_WEBHOOK_URL}" | sed 's#://localstack:4566/#://localhost:4566/#')"
MOCK_SLACK_SCENARIO="${MOCK_SLACK_URL##*/}"

case "${MOCK_SLACK_URL}" in
  http://localhost:4566/restapis/*/slack/*) ;;
  *)
    echo "The detector is not configured with a LocalStack mock Slack URL." >&2
    exit 2
    ;;
esac

reset_mock_slack() {
  curl -fsS -o /dev/null -X POST "${MOCK_SLACK_URL}?reset=true"
}

cleanup() {
  if [ "${KEEP_TEST_DATA}" = "1" ]; then
    return
  fi

  # Remove only the S3 object and DynamoDB records created by this script.
  # このスクリプト自身が作成した S3 オブジェクトと DynamoDB レコードだけを削除する。
  docker exec "${LOCALSTACK_CONTAINER}" awslocal s3 rm "s3://${BUCKET}/${OBJECT_KEY}" >/dev/null 2>&1 || true
  docker exec -i -e TABLE_NAME="${TABLE}" "${LOCALSTACK_CONTAINER}" python3 - "${OBJECT_KEY}" <<'PY' >/dev/null 2>&1 || true
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
        batch.delete_item(Key={"EmailID": item["EmailID"], "recordKey": item["recordKey"]})
PY
  reset_mock_slack >/dev/null 2>&1 || true
}

record_count_for() {
  # PK が EmailID なので Scan ではなく Query で引ける。
  # 強整合読み取りにすることで、検知側と同じ見え方を確認できる。
  docker exec "${LOCALSTACK_CONTAINER}" awslocal dynamodb query \
    --endpoint-url http://dynamodb:8000 \
    --table-name "${TABLE}" \
    --key-condition-expression 'EmailID = :email_id' \
    --expression-attribute-values "{\":email_id\":{\"S\":\"$1\"}}" \
    --consistent-read \
    --select COUNT \
    --query Count \
    --output text 2>/dev/null || true
}

wait_for_record_count() {
  email_id="$1"
  expected_count="$2"
  attempt=1
  while [ "${attempt}" -le 20 ]; do
    count="$(record_count_for "${email_id}")"
    if [ "${count}" = "${expected_count}" ]; then
      return 0
    fi
    sleep 1
    attempt=$((attempt + 1))
  done

  echo "Timed out waiting for ${expected_count} record(s) of EmailID=${email_id}; current count: ${count:-0}" >&2
  return 1
}

wait_for_mock_slack_delivery() {
  attempt=1
  while [ "${attempt}" -le 20 ]; do
    delivery_count="$(docker exec "${LOCALSTACK_CONTAINER}" awslocal dynamodb get-item \
      --endpoint-url http://dynamodb:8000 \
      --table-name mock_slack_api_calls \
      --key "{\"scenario\":{\"S\":\"${MOCK_SLACK_SCENARIO}\"}}" \
      --query 'Item.attempts.N' \
      --output text 2>/dev/null || true)"
    if [ "${delivery_count}" = "1" ]; then
      return 0
    fi
    sleep 1
    attempt=$((attempt + 1))
  done

  echo "Timed out waiting for the mock Slack delivery; current count: ${delivery_count:-0}" >&2
  return 1
}

trap cleanup EXIT HUP INT TERM
reset_mock_slack

echo "Testing same-file duplicate detection"
echo "  duplicated EmailID: ${DUPLICATE_EMAIL_ID} (lines 1 and 3)"
echo "  single EmailID    : ${SINGLE_EMAIL_ID} (line 2)"

# One object, three matching lines. Lines 1 and 3 share the same EmailID.
# 1オブジェクトに3行。1行目と3行目が同じ EmailID を持つ。
docker exec -i "${LOCALSTACK_CONTAINER}" sh -s -- \
  "${DUPLICATE_EMAIL_ID}" "${SINGLE_EMAIL_ID}" "${OBJECT_KEY}" <<'EOS'
set -eu
duplicate_email_id="$1"
single_email_id="$2"
object_key="$3"
iso_timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
log_timestamp="$(date -u '+%Y-%m-%d %H:%M:%S')"
emit() {
  printf '{"date":"%s","log":"[%s] production.INFO: SendEmails [production] success  sending email: %s"}\n' \
    "$iso_timestamp" "$log_timestamp" "$1"
}
{
  emit "$duplicate_email_id"
  emit "$single_email_id"
  emit "$duplicate_email_id"
} > /tmp/mail-duplicate-same-file.log
awslocal s3 cp /tmp/mail-duplicate-same-file.log "s3://mail-send-logs/${object_key}" >/dev/null
EOS

# Two records must exist; one means the sort key collided and overwrote the first.
# 2件でなければならない。1件ならソートキーが衝突して1件目が上書きされている。
wait_for_record_count "${DUPLICATE_EMAIL_ID}" 2
wait_for_record_count "${SINGLE_EMAIL_ID}" 1

# Confirm the two records really did share createdAt and sourceKey, so this test
# actually exercised the collision case rather than passing by accident.
# 2件が本当に createdAt と sourceKey を共有していたことを確認し、
# 偶然通ったのではなく衝突ケースを実際に踏んだことを保証する。
docker exec -i -e TABLE_NAME="${TABLE}" "${LOCALSTACK_CONTAINER}" python3 - "${DUPLICATE_EMAIL_ID}" <<'PY'
import os
import sys

import boto3
from boto3.dynamodb.conditions import Key

table = boto3.resource(
    "dynamodb",
    endpoint_url="http://dynamodb:8000",
    region_name="us-east-1",
    aws_access_key_id="local",
    aws_secret_access_key="local",
).Table(os.environ["TABLE_NAME"])

items = table.query(
    KeyConditionExpression=Key("EmailID").eq(sys.argv[1]),
    ConsistentRead=True,
)["Items"]

created_ats = {item["createdAt"] for item in items}
source_keys = {item["sourceKey"] for item in items}
line_numbers = sorted(int(item["sourceLineNumber"]) for item in items)

for item in items:
    print(f"  recordKey = {item['recordKey']}")

if len(created_ats) != 1 or len(source_keys) != 1:
    print(
        "The two records did not share createdAt/sourceKey, "
        "so the collision case was not exercised.",
        file=sys.stderr,
    )
    raise SystemExit(1)
if line_numbers != [1, 3]:
    print(f"Unexpected line numbers: {line_numbers}", file=sys.stderr)
    raise SystemExit(1)

print("  -> createdAt and sourceKey are identical; sourceLineNumber kept them apart.")
PY

wait_for_mock_slack_delivery

echo "PASS: two lines in one object were stored as two records and notified once."
if [ "${KEEP_TEST_DATA}" = "1" ]; then
  echo "Keeping test data for inspection. Source key: ${OBJECT_KEY}"
else
  echo "Cleaning up this test's S3 object and DynamoDB records."
fi
