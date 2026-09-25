#!/bin/sh
# Verify content-duplicate detection: the same recipient receiving the same body
# in separate email rows, which EmailID matching cannot see.
#
# 本文重複の検知を検証する。別々の EmailID として作られた、同じ宛先・同じ本文の
# メールを見つけられるか。EmailID の一致では原理的に見つからないケースである。

set -eu

LOCALSTACK_CONTAINER="${LOCALSTACK_CONTAINER:-localstack}"
BUCKET="mail-send-logs"
FUNCTION="detect-mail-duplicates"
KEEP_TEST_DATA="${KEEP_TEST_DATA:-0}"

aws_local() {
  docker exec "${LOCALSTACK_CONTAINER}" awslocal "$@"
}

# The detector's own configuration decides which tables and prefix the test must use.
# 対象のテーブルとプレフィックスは検知側の設定から取る。テストが別の場所を見ないようにするため。
ORIGINAL_ENV="$(aws_local lambda get-function-configuration \
  --function-name "${FUNCTION}" --query 'Environment.Variables' --output json)"

read_env() {
  printf '%s' "${ORIGINAL_ENV}" | docker exec -i "${LOCALSTACK_CONTAINER}" python3 -c \
    'import json,sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$1"
}

TABLE="$(read_env TABLE_NAME)"
CONTENT_TABLE="$(read_env CONTENT_TABLE_NAME)"
KEY_PREFIX_BASE="$(read_env TARGET_KEY_PREFIX)"
SLACK_WEBHOOK_URL="$(read_env SLACK_WEBHOOK_URL)"
[ -n "${TABLE}" ] || TABLE="mail_send_log_events"
[ -n "${CONTENT_TABLE}" ] || CONTENT_TABLE="mail_send_log_events_by_content"

MOCK_SLACK_URL="$(printf '%s' "${SLACK_WEBHOOK_URL}" | sed 's#://localstack:4566/#://localhost:4566/#')"
MOCK_SLACK_SCENARIO="${MOCK_SLACK_URL##*/}"

RUN="$(date +%s)$$"
SUFFIX="$(printf '%s' "${RUN}" | cut -c1-15)"
CONTENT_HASH="a${SUFFIX}"
MAIL_TO_HASH="b${SUFFIX}"
SUBJECT_HASH="c${SUFFIX}"
CONTENT_KEY="${MAIL_TO_HASH}:${CONTENT_HASH}"
KEY_PREFIX="${KEY_PREFIX_BASE}content/${RUN}"

FAILURES=0
report() {
  if [ "$2" = "0" ]; then
    echo "  PASS  $1"
  else
    echo "  FAIL  $1" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

# --environment replaces the whole variable map, so always merge into the current one.
# This is the same hazard as production: writing only the variable you care about
# silently drops TABLE_NAME and the detector then points at a table that does not exist.
# --environment は変数マップを全置換する。必ず現在値へマージしてから適用すること。
set_env_var() {
  merged="$(printf '%s' "${ORIGINAL_ENV}" | docker exec -i "${LOCALSTACK_CONTAINER}" python3 -c \
    'import json,sys; env=json.load(sys.stdin); env[sys.argv[1]]=sys.argv[2]; print(json.dumps({"Variables":env}))' "$1" "$2")"
  aws_local lambda update-function-configuration \
    --function-name "${FUNCTION}" --environment "${merged}" >/dev/null
  aws_local lambda wait function-updated-v2 --function-name "${FUNCTION}"
}

restore_env() {
  merged="$(printf '%s' "${ORIGINAL_ENV}" | docker exec -i "${LOCALSTACK_CONTAINER}" python3 -c \
    'import json,sys; print(json.dumps({"Variables": json.load(sys.stdin)}))')"
  aws_local lambda update-function-configuration \
    --function-name "${FUNCTION}" --environment "${merged}" >/dev/null
  aws_local lambda wait function-updated-v2 --function-name "${FUNCTION}"
}

reset_mock_slack() {
  curl -fsS -o /dev/null -X POST "${MOCK_SLACK_URL}?reset=true"
}

slack_deliveries() {
  aws_local dynamodb get-item \
    --endpoint-url http://dynamodb:8000 \
    --table-name mock_slack_api_calls \
    --key "{\"scenario\":{\"S\":\"${MOCK_SLACK_SCENARIO}\"}}" \
    --query 'Item.attempts.N' --output text 2>/dev/null || true
}

delete_records() {
  docker exec -i -e TARGET_TABLE="$1" -e KEY_NAME="$2" "${LOCALSTACK_CONTAINER}" \
    python3 - "${KEY_PREFIX}/" <<'PY' >/dev/null 2>&1 || true
import os, sys, boto3
from boto3.dynamodb.conditions import Attr

key_name = os.environ["KEY_NAME"]
table = boto3.resource(
    "dynamodb", endpoint_url="http://dynamodb:8000", region_name="us-east-1",
    aws_access_key_id="local", aws_secret_access_key="local",
).Table(os.environ["TARGET_TABLE"])

condition = Attr("sourceKey").begins_with(sys.argv[1])
response = table.scan(FilterExpression=condition)
items = response["Items"]
while "LastEvaluatedKey" in response:
    response = table.scan(FilterExpression=condition, ExclusiveStartKey=response["LastEvaluatedKey"])
    items.extend(response["Items"])
with table.batch_writer() as batch:
    for item in items:
        batch.delete_item(Key={key_name: item[key_name], "recordKey": item["recordKey"]})
PY
}

cleanup() {
  restore_env >/dev/null 2>&1 || true
  [ "${KEEP_TEST_DATA}" = "1" ] && return
  aws_local s3 rm "s3://${BUCKET}/${KEY_PREFIX}" --recursive >/dev/null 2>&1 || true
  delete_records "${TABLE}" EmailID
  delete_records "${CONTENT_TABLE}" mail_to_content_hash
  reset_mock_slack >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

# $1 key suffix, $2.. log lines
upload_log() {
  object_key="${KEY_PREFIX}/$1"
  shift
  printf '%s\n' "$@" | docker exec -i "${LOCALSTACK_CONTAINER}" sh -c \
    "cat > /tmp/content-test.log && awslocal s3 cp /tmp/content-test.log s3://${BUCKET}/${object_key} >/dev/null"
}

# $1 email id, $2 command, $3 with-hashes(1/0)
log_line() {
  base="[2026-09-25 01:02:03] production.INFO: $2 [production] success  sending email: $1"
  [ "$3" = "0" ] && { printf '%s' "${base}"; return; }
  printf '%s {"mail_object_type":"App\\\\User","subject":"test","content_hash":"%s","mail_to_hash":"%s","subject_hash":"%s","company":"acme.com","language":"jp","created_at":"2026-09-25 01:00:00"}' \
    "${base}" "${CONTENT_HASH}" "${MAIL_TO_HASH}" "${SUBJECT_HASH}"
}

# $1 expected count, $2 partition key value
# $3 optional attempt limit. A configuration update delays the S3 notification that
# follows it, so callers that just changed the environment need a longer wait.
# 設定変更の直後は S3 通知の配送が遅れるため、呼び出し側で待ち時間を延ばせるようにする。
wait_for_content_count() {
  attempt=1
  limit="${3:-25}"
  while [ "${attempt}" -le "${limit}" ]; do
    count="$(aws_local dynamodb query \
      --endpoint-url http://dynamodb:8000 \
      --table-name "${CONTENT_TABLE}" \
      --key-condition-expression 'mail_to_content_hash = :k' \
      --expression-attribute-values "{\":k\":{\"S\":\"$2\"}}" \
      --consistent-read --select COUNT --query Count --output text 2>/dev/null || true)"
    [ "${count}" = "$1" ] && return 0
    sleep 1
    attempt=$((attempt + 1))
  done
  echo "    expected $1 content record(s), saw ${count:-0}" >&2
  return 1
}

wait_for_slack() {
  attempt=1
  while [ "${attempt}" -le 20 ]; do
    [ "$(slack_deliveries)" = "$1" ] && return 0
    sleep 1
    attempt=$((attempt + 1))
  done
  return 1
}

echo "Testing content-duplicate detection (run ${RUN})"
echo "  content key: ${CONTENT_KEY}"

########################################
echo ""
echo "1. Different EmailIDs, same recipient and body"
########################################
reset_mock_slack
upload_log "pair.log" "$(log_line "${RUN}0" EvenSendEmails 1)" "$(log_line "${RUN}1" OddSendEmails 1)"

if wait_for_content_count 2 "${CONTENT_KEY}"; then report "two content records stored" 0; else report "two content records stored" 1; fi
if wait_for_slack 1; then report "notified exactly once" 0; else report "notified exactly once" 1; fi

RECORD="$(aws_local dynamodb query \
  --endpoint-url http://dynamodb:8000 --table-name "${CONTENT_TABLE}" \
  --key-condition-expression 'mail_to_content_hash = :k' \
  --expression-attribute-values "{\":k\":{\"S\":\"${CONTENT_KEY}\"}}" \
  --consistent-read --max-items 1 --query 'Items[0]' --output json)"

check_attr() {
  actual="$(printf '%s' "${RECORD}" | docker exec -i "${LOCALSTACK_CONTAINER}" python3 -c \
    'import json,sys; print(json.load(sys.stdin).get(sys.argv[1],{}).get("S",""))' "$1")"
  case "${actual}" in
    $2) report "$1 = ${actual}" 0 ;;
    *)  report "$1 expected $2, got '${actual}'" 1 ;;
  esac
}
check_attr emailId "${RUN}*"
check_attr subjectHash "${SUBJECT_HASH}"
check_attr company "acme.com"
check_attr appCreatedAt "2026-09-25 01:00:00"

HAS_EMAILID="$(printf '%s' "${RECORD}" | docker exec -i "${LOCALSTACK_CONTAINER}" python3 -c \
  'import json,sys; print("yes" if "EmailID" in json.load(sys.stdin) else "no")')"
if [ "${HAS_EMAILID}" = "no" ]; then report "EmailID is not a key attribute" 0; else report "EmailID leaked into the key" 1; fi

########################################
echo ""
echo "2. Lines without hashes (logs from before the app change)"
########################################
OLD_ID="9${SUFFIX}"
upload_log "old-1.log" "$(log_line "${OLD_ID}" EvenSendEmails 0)"
upload_log "old-2.log" "$(log_line "${OLD_ID}" EvenSendEmails 0)"

attempt=1
while [ "${attempt}" -le 25 ]; do
  legacy_count="$(aws_local dynamodb query \
    --endpoint-url http://dynamodb:8000 --table-name "${TABLE}" \
    --key-condition-expression 'EmailID = :k' \
    --expression-attribute-values "{\":k\":{\"S\":\"${OLD_ID}\"}}" \
    --consistent-read --select COUNT --query Count --output text 2>/dev/null || true)"
  [ "${legacy_count}" = "2" ] && break
  sleep 1
  attempt=$((attempt + 1))
done
if [ "${legacy_count:-0}" = "2" ]; then report "EmailID detection still works without hashes" 0; else report "EmailID detection still works without hashes" 1; fi

CONTENT_TOTAL="$(aws_local dynamodb scan \
  --endpoint-url http://dynamodb:8000 --table-name "${CONTENT_TABLE}" \
  --filter-expression 'contains(sourceKey, :p)' \
  --expression-attribute-values "{\":p\":{\"S\":\"${KEY_PREFIX}/old-\"}}" \
  --select COUNT --query Count --output text 2>/dev/null || echo "?")"
if [ "${CONTENT_TOTAL}" = "0" ]; then report "no content record for hashless lines" 0; else report "hashless lines reached the content table (${CONTENT_TOTAL})" 1; fi

# The two objects above share one EmailID, so this case also fires a notification.
# Drain it before the next case resets the counter, or it lands in that case's count.
# 上の2オブジェクトは EmailID が同じなので、このケースでも通知が飛ぶ。
# 次のケースがカウンタをリセットする前に回収しておかないと、次の件数に混ざる。
wait_for_slack 1 || true

########################################
echo ""
echo "3. Suppression by subject hash"
########################################
set_env_var SUPPRESS_SUBJECT_HASHES "${SUBJECT_HASH}"
# Let the replaced Lambda container settle before the upload; the S3 notification
# that immediately follows a configuration update can take far longer to arrive.
# 差し替わった Lambda コンテナが落ち着くのを待つ。設定変更の直後の S3 通知は配送が遅い。
sleep 5
reset_mock_slack
SUP_KEY="${MAIL_TO_HASH}:s${SUFFIX}"
upload_log "suppressed.log" \
  "$(log_line "${RUN}2" EvenSendEmails 1 | sed "s/${CONTENT_HASH}/s${SUFFIX}/")" \
  "$(log_line "${RUN}3" OddSendEmails 1 | sed "s/${CONTENT_HASH}/s${SUFFIX}/")"

if wait_for_content_count 2 "${SUP_KEY}" 60; then report "records are still stored while suppressed" 0; else report "records are still stored while suppressed" 1; fi
sleep 5
if [ "$(slack_deliveries)" = "None" ] || [ -z "$(slack_deliveries)" ]; then
  report "no Slack notification for a suppressed subject" 0
else
  report "a suppressed subject was notified ($(slack_deliveries))" 1
fi
restore_env

########################################
echo ""
echo "4. The detection window is configurable"
########################################
set_env_var WINDOW_MINUTES "30"
WINDOW_SEEN="$(aws_local lambda get-function-configuration --function-name "${FUNCTION}" \
  --query 'Environment.Variables.WINDOW_MINUTES' --output text)"
if [ "${WINDOW_SEEN}" = "30" ]; then report "WINDOW_MINUTES is applied" 0; else report "WINDOW_MINUTES is applied" 1; fi
restore_env
WINDOW_SEEN="$(aws_local lambda get-function-configuration --function-name "${FUNCTION}" \
  --query 'Environment.Variables.TABLE_NAME' --output text)"
if [ "${WINDOW_SEEN}" = "${TABLE}" ]; then report "restoring the env kept TABLE_NAME" 0; else report "TABLE_NAME was lost on restore" 1; fi

########################################
echo ""
if [ "${FAILURES}" -eq 0 ]; then
  echo "PASS: content-duplicate detection behaves as specified."
else
  echo "FAIL: ${FAILURES} check(s) did not pass." >&2
  exit 1
fi
