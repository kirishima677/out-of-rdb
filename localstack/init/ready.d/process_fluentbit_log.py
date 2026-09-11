"""Process a Fluent Bit S3 object and record its basic metadata in DynamoDB Local."""

import json
import os
from urllib.parse import unquote_plus

import boto3

REGION = "us-east-1"
TABLE_NAME = "processed_logs"


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

    processed = 0
    for record in event["Records"]:
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
        line_count = len([line for line in body.splitlines() if line])
        table.put_item(
            Item={
                "object_key": key,
                "bucket": bucket,
                "line_count": line_count,
                "content": body,
            }
        )
        print(f"Processed s3://{bucket}/{key}: {line_count} log line(s)")
        processed += 1

    return {"processed_objects": processed}


def _get_or_create_table(dynamodb):
    try:
        return dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "object_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "object_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    except dynamodb.meta.client.exceptions.ResourceInUseException:
        return dynamodb.Table(TABLE_NAME)
