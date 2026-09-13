"""Show mail-duplicate detection records stored in DynamoDB Local.

DynamoDB Local に保存されたメール重複検知の履歴を表示する。
"""

import argparse
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

TABLE_NAME = "mail_duplicate_events"
DEFAULT_ENDPOINT = "http://dynamodb:8000"


def main():
    """Read records and print them in reverse chronological order.

    レコードを読み取り、新しい検知日時順に表示する。
    """
    args = _parse_args()
    table = _get_table()

    try:
        items = _read_items(table, args.email_id)
    except ClientError as error:
        if error.response["Error"]["Code"] == "ResourceNotFoundException":
            print(f"Table not found: {TABLE_NAME}（まだ検知履歴はありません）")
            return
        raise

    items.sort(key=lambda item: item["createdAt"], reverse=True)
    items = items[: args.limit]

    if not items:
        print("No mail-duplicate detection records found.（検知履歴はありません）")
        return

    print(f"{len(items)} record(s) in {TABLE_NAME}:")
    for item in items:
        print("-" * 72)
        print(f"EmailID    : {item['EmailID']}")
        print(f"DetectedAt : {item['createdAt']}")
        print(f"ExpiresAt  : {_format_epoch(item.get('expiresAt'))}")
        print(f"Source     : s3://{item.get('sourceBucket', '?')}/{item.get('sourceKey', '?')}")


def _parse_args():
    """Parse optional filters for local inspection.

    ローカル確認用の任意フィルターを解析する。
    """
    parser = argparse.ArgumentParser(
        description="Show records in DynamoDB Local mail_duplicate_events."
    )
    parser.add_argument(
        "--email-id", help="Show only records for this EmailID.（指定した EmailID のみ表示）"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum records to display; default: 100.（表示上限、既定: 100）",
    )
    return parser.parse_args()


def _get_table():
    """Connect to DynamoDB Local using local dummy credentials.

    ローカル用のダミー認証情報を使って DynamoDB Local へ接続する。
    """
    dynamodb = boto3.resource(
        "dynamodb",
        endpoint_url=os.environ.get("DYNAMODB_ENDPOINT", DEFAULT_ENDPOINT),
        region_name="us-east-1",
        aws_access_key_id="local",
        aws_secret_access_key="local",
    )
    return dynamodb.Table(TABLE_NAME)


def _read_items(table, email_id):
    """Use a keyed query when possible; otherwise scan the local table.

    EmailID 指定時は Query、それ以外はローカルテーブルを Scan する。
    """
    if email_id:
        return table.query(
            KeyConditionExpression=Key("EmailID").eq(email_id),
            ConsistentRead=True,
        )["Items"]

    items = []
    response = table.scan(ConsistentRead=True)
    items.extend(response["Items"])
    while "LastEvaluatedKey" in response:
        response = table.scan(
            ConsistentRead=True, ExclusiveStartKey=response["LastEvaluatedKey"]
        )
        items.extend(response["Items"])
    return items


def _format_epoch(value):
    """Format a DynamoDB epoch value as UTC, or show a missing value.

    DynamoDB のエポック秒を UTC 表記に変換し、未設定ならその旨を表示する。
    """
    if value is None:
        return "-"
    return datetime.fromtimestamp(int(value), timezone.utc).isoformat()


if __name__ == "__main__":
    main()
