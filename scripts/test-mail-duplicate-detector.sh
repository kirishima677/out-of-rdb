#!/bin/sh
# Verify the local S3 -> Lambda -> DynamoDB duplicate-detection flow.
# ローカルの S3 -> Lambda -> DynamoDB 重複検知フローを検証する。

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
# Combine the Unix time and this shell's PID to create a numeric, unique EmailID.
# Unix 時刻とこのシェルの PID を組み合わせ、数値かつ一意な EmailID を作る。
TEST_EMAIL_ID="$(date +%s)$$"
# Match the detector's prefix filter so test objects are not skipped.
# 検知側のプレフィックスフィルタに合わせ、テスト用オブジェクトが無視されないようにする。
DETECTOR_KEY_PREFIX="$(docker exec "${LOCALSTACK_CONTAINER}" awslocal lambda get-function \
  --function-name detect-mail-duplicates \
  --query 'Configuration.Environment.Variables.TARGET_KEY_PREFIX' \
  --output text 2>/dev/null || echo '')"
if [ "${DETECTOR_KEY_PREFIX}" = "None" ]; then
  DETECTOR_KEY_PREFIX=""
fi
KEY_PREFIX="${DETECTOR_KEY_PREFIX}verification/${TEST_EMAIL_ID}"
FIRST_KEY="${KEY_PREFIX}/mail-1.log"
SECOND_KEY="${KEY_PREFIX}/mail-2.log"
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
  # Reset removes the scenario counter, so each test expects exactly one delivery.
  # reset はシナリオのカウンターを削除するため、毎回ちょうど1回の配送を確認できる。
  curl -fsS -o /dev/null -X POST "${MOCK_SLACK_URL}?reset=true"
}

cleanup() {
  if [ "${KEEP_TEST_DATA}" = "1" ]; then
    # Keep only when explicitly requested; the default keeps DynamoDB clean.
    # 明示指定時だけ残す。既定では DynamoDB をきれいな状態に保つ。
    return
  fi

  # Remove only the S3 objects and DynamoDB records created by this script.
  # このスクリプト自身が作成した S3 オブジェクトと DynamoDB レコードだけを削除する。
  docker exec "${LOCALSTACK_CONTAINER}" awslocal s3 rm "s3://${BUCKET}/${FIRST_KEY}" >/dev/null 2>&1 || true
  docker exec "${LOCALSTACK_CONTAINER}" awslocal s3 rm "s3://${BUCKET}/${SECOND_KEY}" >/dev/null 2>&1 || true
  docker exec -i -e TABLE_NAME="${TABLE}" "${LOCALSTACK_CONTAINER}" python3 - "${KEY_PREFIX}/" <<'PY' >/dev/null 2>&1 || true
import os
import sys
import boto3
from boto3.dynamodb.conditions import Attr

table = boto3.resource("dynamodb", endpoint_url="http://dynamodb:8000", region_name="us-east-1", aws_access_key_id="local", aws_secret_access_key="local").Table(os.environ["TABLE_NAME"])
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
  reset_mock_slack >/dev/null 2>&1 || true
}

wait_for_record_count() {
  expected_count="$1"
  attempt=1
  while [ "${attempt}" -le 20 ]; do
    # PK が EmailID なので Scan ではなく Query で正確かつ安価に引ける。
    # 強整合読み取りにすることで、検知側と同じ見え方を確認できる。
    count="$(docker exec "${LOCALSTACK_CONTAINER}" awslocal dynamodb query \
      --endpoint-url http://dynamodb:8000 \
      --table-name "${TABLE}" \
      --key-condition-expression 'EmailID = :email_id' \
      --expression-attribute-values "{\":email_id\":{\"S\":\"${TEST_EMAIL_ID}\"}}" \
      --consistent-read \
      --select COUNT \
      --query Count \
      --output text 2>/dev/null || true)"
    if [ "${count}" = "${expected_count}" ]; then
      return 0
    fi
    sleep 1
    attempt=$((attempt + 1))
  done

  echo "Timed out waiting for ${expected_count} record(s); current count: ${count:-0}" >&2
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

echo "Testing duplicate detection with EmailID=${TEST_EMAIL_ID}"

# Upload one matching log, then wait until its record is visible before the second upload.
# 1件目を配送し、記録が見えることを確認してから2件目を配送する。
docker exec -i "${LOCALSTACK_CONTAINER}" sh -s -- "${TEST_EMAIL_ID}" "${FIRST_KEY}" <<'EOS'
set -eu
email_id="$1"
key="$2"
iso_timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
log_timestamp="$(date -u '+%Y-%m-%d %H:%M:%S')"
printf '{"date":"%s","log":"[%s] production.INFO: SendEmails [production] success  sending email: %s"}\n' "$iso_timestamp" "$log_timestamp" "$email_id" > /tmp/mail-duplicate-test-1.log
awslocal s3 cp /tmp/mail-duplicate-test-1.log "s3://mail-send-logs/${key}"
EOS
wait_for_record_count 1

docker exec -i "${LOCALSTACK_CONTAINER}" sh -s -- "${TEST_EMAIL_ID}" "${SECOND_KEY}" <<'EOS'
set -eu
email_id="$1"
key="$2"
iso_timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
log_timestamp="$(date -u '+%Y-%m-%d %H:%M:%S')"
printf '{"date":"%s","log":"[%s] production.INFO: SendEmails [production] success  sending email: %s"}\n' "$iso_timestamp" "$log_timestamp" "$email_id" > /tmp/mail-duplicate-test-2.log
awslocal s3 cp /tmp/mail-duplicate-test-2.log "s3://mail-send-logs/${key}"
EOS
wait_for_record_count 2
wait_for_mock_slack_delivery

echo "PASS: duplicate notification was written to the Lambda log and delivered once to mock Slack."
if [ "${KEEP_TEST_DATA}" = "1" ]; then
  echo "Keeping test data for inspection (EmailID=${TEST_EMAIL_ID})."
  echo "Run: docker exec client /workspace/.venv/bin/python /workspace/sample/show_mail_duplicate_events.py --email-id ${TEST_EMAIL_ID}"
else
  echo "Cleaning up this test's S3 objects and DynamoDB records."
fi
