"""Store complete mail-send log records and report the rolling duplicate scale.

メール送信ログの1レコード全体を保存し、直近1時間の重複規模を通知する。
"""

import gzip
import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote_plus

import boto3
from boto3.dynamodb.conditions import Key

REGION = "ap-northeast-1"
TABLE_NAME = "mail_send_log_events"
EMAIL_INDEX = "email-detected-at-index"
WINDOW_INDEX = "window-bucket-detected-at-index"
WINDOW = timedelta(hours=1)

SUCCESSFUL_SEND = re.compile(
    r"\b(?P<command>EvenSendEmails|OddSendEmails|SendEmails)\b"
    r"\s+\[[^\]]+\]\s+success\s+sending email:\s*"
    r"(?P<email_id>\d+)\b"
)


def handler(event, _context):
    """Save each matching record and notify with aggregate scale only.

    一致したログをすべて保存し、通知には集計規模だけを載せる。
    """
    print("Received S3 event:", json.dumps(event))
    s3 = boto3.client("s3", endpoint_url=os.environ["LOCALSTACK_ENDPOINT"], region_name=REGION)
    dynamodb = boto3.resource(
        "dynamodb", endpoint_url=os.environ["DYNAMODB_ENDPOINT"], region_name=REGION,
        aws_access_key_id="local", aws_secret_access_key="local",
    )
    table = _get_or_create_table(dynamodb)
    saved_records = []
    triggered_duplicate = False

    for s3_record in event["Records"]:
        bucket = s3_record["s3"]["bucket"]["name"] # TODO バケットのパスの指定
        key = unquote_plus(s3_record["s3"]["object"]["key"])
        for log_record in _extract_log_records(_read_s3_object(s3, bucket, key), key):
            detected_at = datetime.now(timezone.utc)
            item = _build_item(bucket, key, log_record, detected_at)
            table.put_item(Item=item)
            saved_records.append(item)
            if _recent_email_record_count(table, item["EmailID"], detected_at - WINDOW, item) >= 2:  # 最近の同一EmailIDのレコードが2件以上なら重複とみなす
                triggered_duplicate = True

    # 重複が検知された場合はSlackへ通知
    if triggered_duplicate:
        stats = _rolling_duplicate_stats(table, datetime.now(timezone.utc) - WINDOW, saved_records)
        if stats["duplicate_email_ids"]:
            _notify_duplicate_scale(stats, datetime.now(timezone.utc))
    return {"saved_successful_send_logs": len(saved_records), "duplicate_notifications": int(triggered_duplicate)}


def _read_s3_object(s3, bucket, key):
    """Read an S3 object and transparently expand gzip input.

    S3 オブジェクトを読み込み、gzip 入力なら透過的に展開する。
    """
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    if key.endswith(".gz"):
        body = gzip.decompress(body)
    return body.decode("utf-8", errors="replace")


def _extract_log_records(body, key):
    """Yield complete fields for every successful mail-send log line.

    メール送信成功に一致する各ログ行について、完全な保存用フィールドを返す。
    """
    for line_number, raw_line in enumerate(body.splitlines(), start=1):
        parsed_line, log_line = _unwrap_json_log(raw_line)
        match = SUCCESSFUL_SEND.search(log_line)
        if not match:
            continue
        print("Matched successful mail send:", json.dumps({"EmailID": match["email_id"], "command": match["command"], "key": key, "lineNumber": line_number}))
        yield {
            "EmailID": match["email_id"], "command": match["command"], "sourceLineNumber": line_number,
            "rawLog": raw_line, "logMessage": log_line,
            "logTimestamp": parsed_line.get("date") if parsed_line else None,
        }


def _unwrap_json_log(line):
    """Return parsed Fluent Bit fields and the Laravel message, or a raw line.

    Fluent Bit JSON Lines の項目と Laravel メッセージを返し、それ以外は元の行を返す。
    """
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None, line
    if isinstance(parsed, dict) and isinstance(parsed.get("log"), str):
        return parsed, parsed["log"]
    return parsed if isinstance(parsed, dict) else None, line


def _build_item(bucket, key, log_record, detected_at):
    """Build one complete DynamoDB log record with lookup index attributes.

    検索用インデックス属性を含む、完全な DynamoDB ログレコードを作る。
    """
    record_identity = f"{bucket}\0{key}\0{log_record['sourceLineNumber']}"
    item = {
        "recordId": hashlib.sha256(record_identity.encode("utf-8")).hexdigest(),
        "EmailID": log_record["EmailID"],
        "detectedAt": detected_at.isoformat(timespec="microseconds"),
        "windowBucket": detected_at.strftime("%Y-%m-%dT%H"),
        "expiresAt": int((detected_at + WINDOW).timestamp()),
        "command": log_record["command"], "sourceBucket": bucket, "sourceKey": key,
        "sourceLineNumber": log_record["sourceLineNumber"], "rawLog": log_record["rawLog"],
        "logMessage": log_record["logMessage"],
    }
    if log_record["logTimestamp"]:
        item["logTimestamp"] = log_record["logTimestamp"]
    return item


def _get_or_create_table(dynamodb):
    """Create the full-record table and its duplicate/rolling-window indexes.

    全レコード表と、重複判定・直近1時間集計用のインデックスを作成する。
    """
    try:
        table = dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "recordId", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "recordId", "AttributeType": "S"},
                {"AttributeName": "EmailID", "AttributeType": "S"},
                {"AttributeName": "detectedAt", "AttributeType": "S"},
                {"AttributeName": "windowBucket", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {"IndexName": EMAIL_INDEX, "KeySchema": [{"AttributeName": "EmailID", "KeyType": "HASH"}, {"AttributeName": "detectedAt", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "ALL"}},
                {"IndexName": WINDOW_INDEX, "KeySchema": [{"AttributeName": "windowBucket", "KeyType": "HASH"}, {"AttributeName": "detectedAt", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "ALL"}},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
    except dynamodb.meta.client.exceptions.ResourceInUseException:
        table = dynamodb.Table(TABLE_NAME)
    _enable_ttl(dynamodb.meta.client)
    return table


def _enable_ttl(client):
    """Enable one-hour record cleanup through the expiresAt TTL attribute.

    expiresAt 属性を使った1時間後のレコード自動削除を有効化する。
    """
    response = client.describe_time_to_live(TableName=TABLE_NAME)
    if response["TimeToLiveDescription"].get("TimeToLiveStatus") == "ENABLED":
        return
    client.update_time_to_live(
        TableName=TABLE_NAME,
        TimeToLiveSpecification={"Enabled": True, "AttributeName": "expiresAt"},
    )


def _recent_email_record_count(table, email_id, window_start, current_item):
    """Count an EmailID in the rolling window, including the just-saved item.

    保存直後のレコードを必ず含めて、直近1時間の同一 EmailID 件数を数える。
    """
    response = table.query(IndexName=EMAIL_INDEX, KeyConditionExpression=Key("EmailID").eq(email_id) & Key("detectedAt").gte(window_start.isoformat(timespec="microseconds")))
    record_ids = {item["recordId"] for item in response["Items"]}
    record_ids.add(current_item["recordId"])
    return len(record_ids)


def _rolling_duplicate_stats(table, window_start, current_items):
    """Aggregate duplicate scale for the complete rolling one-hour window.

    直近1時間全体の重複規模を集計する。
    """
    now = datetime.now(timezone.utc)
    buckets = {window_start.strftime("%Y-%m-%dT%H"), now.strftime("%Y-%m-%dT%H")}
    records_by_id = {item["recordId"]: item for item in current_items}
    for bucket in buckets:
        response = table.query(IndexName=WINDOW_INDEX, KeyConditionExpression=Key("windowBucket").eq(bucket) & Key("detectedAt").gte(window_start.isoformat(timespec="microseconds")))
        records_by_id.update({item["recordId"]: item for item in response["Items"]})
    counts = Counter(item["EmailID"] for item in records_by_id.values())
    duplicate_counts = [count for count in counts.values() if count >= 2]
    return {"duplicate_email_ids": len(duplicate_counts), "duplicate_log_events": sum(count - 1 for count in duplicate_counts), "window_start": window_start, "window_end": now}


def _notify_duplicate_scale(stats, detected_at):
    """Notify Slack with scale only; individual EmailIDs are deliberately omitted.

    個別 EmailID を意図的に載せず、規模だけを Slack へ通知する。
    """
    message = (
        "⚠️ メール送信重複を検知しました\n"
        f"対象期間(UTC): {stats['window_start'].isoformat()} - {stats['window_end'].isoformat()}\n"
        f"重複対象 EmailID 数: {stats['duplicate_email_ids']}\n"
        f"2件目以降の重複ログ件数: {stats['duplicate_log_events']}\n"
        f"検知日時(UTC): {detected_at.isoformat()}"
    )
    print(message)
    _post_to_slack(message)


def _post_to_slack(message):
    """Post a notification when a Slack webhook URL is configured.

    Slack Webhook URL が設定されている場合に通知を送信する。
    """
    from urllib import error, request

    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        print("SLACK_WEBHOOK_URL is not set; skip Slack notification.")
        return
    webhook_request = request.Request(webhook_url, data=json.dumps({"text": message}).encode("utf-8"), headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    try:
        with request.urlopen(webhook_request, timeout=5) as response:
            print(f"Slack notification sent: HTTP {response.status}")
    except error.URLError as exc:
        print(f"Slack notification failed: {exc}")
