"""Provide controllable Slack-like HTTP responses for local retry tests.

ローカルのリトライ試験用に、応答を制御できる Slack 風 HTTP API を提供する。
"""

import json
import os

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
TABLE_NAME = "mock_slack_api_calls"
SUPPORTED_MODES = {
    "always-success",
    "always-failure",
    "fail-twice-then-success",
}


def handler(event, _context):
    """Return a configured response and persist its invocation count.

    指定された応答を返し、呼び出し回数を永続化する。
    """
    mode = (event.get("pathParameters") or {}).get("mode")
    query = event.get("queryStringParameters") or {}
    if mode not in SUPPORTED_MODES:
        return _response(404, {"message": f"Unknown mock Slack mode: {mode}"})

    table = _get_or_create_table()
    if query.get("reset") == "true":
        # Reset does not count as a Slack delivery attempt.
        # reset 呼び出しは Slack 配送試行の回数として数えない。
        table.delete_item(Key={"scenario": mode})
        return _response(204, {})

    attempt = _increment_attempt(table, mode)
    print(
        "Mock Slack request:",
        json.dumps({"mode": mode, "attempt": attempt, "body": event.get("body")}),
    )

    if mode == "always-success":
        return _response(200, {"ok": True, "attempt": attempt})
    if mode == "always-failure":
        return _response(500, {"ok": False, "attempt": attempt})
    if attempt <= 2:
        return _response(500, {"ok": False, "attempt": attempt})
    return _response(200, {"ok": True, "attempt": attempt})


def _get_or_create_table():
    """Return the DynamoDB Local table that stores scenario counters.

    シナリオごとのカウンターを保存する DynamoDB Local テーブルを返す。
    """
    dynamodb = boto3.resource(
        "dynamodb",
        endpoint_url=os.environ["DYNAMODB_ENDPOINT"],
        region_name=REGION,
        aws_access_key_id="local",
        aws_secret_access_key="local",
    )
    try:
        return dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "scenario", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "scenario", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    except dynamodb.meta.client.exceptions.ResourceInUseException:
        return dynamodb.Table(TABLE_NAME)


def _increment_attempt(table, mode):
    """Atomically increment and return the scenario attempt counter.

    シナリオの試行回数を原子的に加算し、その値を返す。
    """
    response = table.update_item(
        Key={"scenario": mode},
        UpdateExpression="ADD attempts :one",
        ExpressionAttributeValues={":one": 1},
        ReturnValues="UPDATED_NEW",
    )
    return int(response["Attributes"]["attempts"])


def _response(status_code, body):
    """Build an API Gateway proxy response.

    API Gateway プロキシ統合向けのレスポンスを作る。
    """
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
