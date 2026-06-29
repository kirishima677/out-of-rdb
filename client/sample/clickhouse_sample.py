"""Simple ClickHouse sample.

Requires:
    pip install clickhouse-connect

Run from host:
    python3 client/sample/clickhouse_sample.py

Run from the client container:
    CLICKHOUSE_HOST=clickhouse python /workspace/sample/clickhouse_sample.py
"""

import os
import clickhouse_connect

host = os.getenv("CLICKHOUSE_HOST", "localhost")
port = int(os.getenv("CLICKHOUSE_PORT", "8123"))
username = os.getenv("CLICKHOUSE_USER", "default")
password = os.getenv("CLICKHOUSE_PASSWORD", "")
database = os.getenv("CLICKHOUSE_DATABASE", "default")

client = clickhouse_connect.get_client(
    host=host,
    port=port,
    username=username,
    password=password,
    database=database,
)

print(f"Connected to ClickHouse: {host}:{port}")
print("Version:", client.server_version)

client.command(
    """
    CREATE TABLE IF NOT EXISTS users (
        id UInt32,
        name String,
        age UInt8
    )
    ENGINE = MergeTree
    ORDER BY id
    """
)

client.command("TRUNCATE TABLE users")

client.insert(
    "users",
    [
        (1, "Alice", 25),
        (2, "Bob", 31),
        (3, "Charlie", 28),
    ],
    column_names=["id", "name", "age"],
)

rows = client.query("SELECT id, name, age FROM users ORDER BY id").result_rows

print("\nUsers")
print("-----")
for row in rows:
    print(row)

count = client.query("SELECT count() FROM users").first_item
print(f"\nTotal users: {count}")
