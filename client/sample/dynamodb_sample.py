"""DynamoDB Local にテーブル作成・書き込み・読み取りを行うサンプル。"""

import boto3
from botocore.exceptions import ClientError

TABLE_NAME = "users"

dynamodb = boto3.resource(
    "dynamodb",
    endpoint_url="http://dynamodb:8000",
    region_name="us-east-1",
    aws_access_key_id="local",
    aws_secret_access_key="local",
)

try:
    table = dynamodb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()
    print(f"Created table: {TABLE_NAME}")
except ClientError as error:
    if error.response["Error"]["Code"] != "ResourceInUseException":
        raise
    table = dynamodb.Table(TABLE_NAME)

table.put_item(Item={"user_id": "alice", "name": "Alice", "age": 30})
response = table.get_item(Key={"user_id": "alice"})
print("Get item:", response.get("Item"))
