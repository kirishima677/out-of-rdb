"""Detect duplicate successful mail-send logs delivered to S3.

S3 に配送されたメール送信成功ログから、重複した送信を検知する。

This initial implementation intentionally does not deduplicate S3 events or
Lambda retries. It follows the documented "sending-side duplicate" rule only:
the same EmailID appearing at least twice within the detection-time window.

この初期実装では S3 イベントや Lambda 再試行の重複排除はしない。
検知時刻からの時間枠内に同じ EmailID が 2 回以上現れるという、
「送信側重複」の判定だけを扱う。
"""

import gzip
import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote_plus

import boto3
from boto3.dynamodb.conditions import Key

REGION = "us-east-1"
TABLE_NAME = "mail_duplicate_events"
WINDOW = timedelta(hours=1)

SUCCESSFUL_SEND = re.compile(
    r"\b(?P<command>EvenSendEmails|OddSendEmails|SendEmails)\b"
    r"\s+\[[^\]]+\]\s+success\s+sending email:\s*"
    r"(?P<email_id>\d+)\b"
)


def handler(event, _context):
    print("Received S3 event:", json.dumps(event))
    s3 = boto3.client(
        "s3", endpoint_url=os.environ["LOCALSTACK_ENDPOINT"], region_name=REGION
    )
    dynamodb = boto3.resource(
        "dynamodb",
        endpoint_url=os.environ["DYNAMODB_ENDPOINT"],
        region_name=REGION,
        aws_access_key_id="local",
        aws_secret_access_key="local",
    )
    table = _get_or_create_table(dynamodb)

    saved_count = 0
    notified_ids = set()
    for record in event["Records"]:
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])
        for email_id in _extract_email_ids(_read_s3_object(s3, bucket, key), key):
            detected_at = datetime.now(timezone.utc)
            created_at = detected_at.isoformat(timespec="microseconds")
            table.put_item(
                Item={
                    "EmailID": email_id,
                    "createdAt": created_at,
                    "expiresAt": int((detected_at + WINDOW).timestamp()),
                    "sourceBucket": bucket,
                    "sourceKey": key,
                }
            )
            saved_count += 1

            occurrences = _count_recent_occurrences(
                table, email_id, detected_at - WINDOW
            )
            if occurrences >= 2 and email_id not in notified_ids:
                _notify_duplicate(email_id, occurrences, detected_at)
                notified_ids.add(email_id)

    return {
        "saved_successful_send_logs": saved_count,
        "duplicate_notifications": len(notified_ids),
    }


def _read_s3_object(s3, bucket, key):
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    if key.endswith(".gz"):
        body = gzip.decompress(body)
    return body.decode("utf-8", errors="replace")


def _extract_email_ids(body, key):
    for raw_line in body.splitlines():
        log_line = _unwrap_json_log(raw_line)
        match = SUCCESSFUL_SEND.search(log_line)
        if match:
            print(
                "Matched successful mail send:",
                json.dumps(
                    {"EmailID": match["email_id"], "command": match["command"], "key": key}
                ),
            )
            yield match["email_id"]


def _unwrap_json_log(line):
    """Return a Laravel message from Fluent Bit JSON Lines, or a raw line.

    Fluent Bit の JSON Lines なら Laravel のメッセージを返し、それ以外は
    元のログ行を返す。
    """
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return line
    return parsed.get("log", line) if isinstance(parsed, dict) else line


def _get_or_create_table(dynamodb):
    try:
        return dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "EmailID", "KeyType": "HASH"},
                {"AttributeName": "createdAt", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "EmailID", "AttributeType": "S"},
                {"AttributeName": "createdAt", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
    except dynamodb.meta.client.exceptions.ResourceInUseException:
        return dynamodb.Table(TABLE_NAME)


def _count_recent_occurrences(table, email_id, window_start):
    response = table.query(
        KeyConditionExpression=Key("EmailID").eq(email_id)
        & Key("createdAt").gte(window_start.isoformat(timespec="microseconds")),
        ConsistentRead=True,
        Select="COUNT",
    )
    return response["Count"]


def _notify_duplicate(email_id, occurrences, detected_at):
    message = (
        "⚠️ メール重複を検知しました "
        f"EmailID: {email_id} / 件数: {occurrences} / "
        f"検知日時(UTC): {detected_at.isoformat()}"
    )
    print(message)

    _post_to_slack(message)


def _post_to_slack(message):
    """Post a plain-text notification when a Slack webhook URL is configured.

    Slack Webhook URL が設定されている場合にプレーンテキストの通知を送信する。
    """
    from urllib import error, request

    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        # Allow log-only operation when a notification target is not configured.
        # 通知先未設定時は Lambda ログ出力だけで処理を続行する。
        print("SLACK_WEBHOOK_URL is not set; skip Slack notification.")
        return

    payload = json.dumps({"text": message}).encode("utf-8")
    webhook_request = request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with request.urlopen(webhook_request, timeout=5) as response:
            print(f"Slack notification sent: HTTP {response.status}")
    except error.URLError as exc:
        # Do not fail the log-processing invocation only because Slack failed.
        # Slack への送信失敗だけでログ処理全体を失敗させない。
        print(f"Slack notification failed: {exc}")
