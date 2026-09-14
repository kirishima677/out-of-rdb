"""Detect duplicate mail sends from S3-stored send logs and notify Slack.

S3 に保存されたメール送信ログから同一 EmailID の重複送信を検知し、Slack へ通知する。

データモデル:
  ログ1行につき1レコードを保存する追記型。PK=EmailID / SK=recordKey の複合キーにより、
  同じ EmailID の出現が上書きされず別レコードとして積み上がる。
  これにより「1時間ローリングでの判定」と「何件目か」が同時に取れる。

  PK が EmailID なのでベーステーブルを直接 Query でき、ConsistentRead=True を指定できる。
  GSI は仕様上 ConsistentRead を指定できず常に結果整合性のため、判定経路には使わない。
  アプリサーバーが 2〜3 台あり Lambda が同時起動する環境では、これが検知漏れの分かれ目になる。
"""

import gzip
import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib import error, request
from urllib.parse import unquote_plus

import boto3
from boto3.dynamodb.conditions import Key

REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
TABLE_NAME = "mail_send_log_events"

# 判定窓。同一 EmailID がこの範囲に 2 件以上あれば重複とする。
WINDOW = timedelta(hours=1)

# Slack 本文の表示用タイムゾーン。保存は常に UTC。
DISPLAY_TZ = timezone(timedelta(hours=9), "JST")

# 対象オブジェクトのプレフィックス。
# S3 イベント通知側でも絞ること。ここは設定漏れに備えた保険。
# 例: "service=official-alumni/env=production/log_source=laravel-app/"
TARGET_KEY_PREFIX = os.environ.get("TARGET_KEY_PREFIX", "")

# 行頭からの一致のみを許す。
# Laravel のログ末尾には context JSON が付き、そこにメールの subject
# (ユーザー由来の文字列) が入るため、行中一致にすると件名に文字列を仕込むことで
# 偽の重複を発生させられる。前置部分は任意グループなので、Fluent Bit が
# log フィールドをメッセージ部だけに加工していても動く。
SUCCESSFUL_SEND = re.compile(
    r"^(?:\[(?P<log_timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+\w+\.INFO:\s+)?"
    r"(?P<command>EvenSendEmails|OddSendEmails|SendEmails)\s+"
    r"\[[^\]]*\]\s+success\s+sending email:\s*"
    r"(?P<email_id>\d+)\b"
)


def _aws_kwargs(endpoint_env):
    """Build client kwargs, attaching local credentials only for LocalStack.

    LocalStack 向けのときだけ、ローカル用の認証情報を付けた設定を作る。
    """
    kwargs = {"region_name": REGION}
    endpoint = os.environ.get(endpoint_env)
    if endpoint:
        kwargs.update(
            endpoint_url=endpoint,
            aws_access_key_id="local",
            aws_secret_access_key="local",
        )
    return kwargs


# ウォームスタート間で TCP 接続を再利用するためモジュールスコープに置く。
S3 = boto3.client("s3", **_aws_kwargs("LOCALSTACK_ENDPOINT"))
DYNAMODB = boto3.resource("dynamodb", **_aws_kwargs("DYNAMODB_ENDPOINT"))


def handler(event, _context):
    """Save one record per log line, count occurrences, and notify on duplicates.

    ログ1行ごとに1レコードを保存し、出現件数を数え、重複があれば通知する。
    """
    print("Received S3 event:", json.dumps(event))

    # 実行中の「現在時刻」を1つに固定し、保存時刻・判定窓・通知時刻を揃える。
    # この結果、同一実行で処理した全レコードの createdAt が同値になるため、
    # SK の一意性は sourceKey と sourceLineNumber が担保する（_build_item 参照）。
    now = datetime.now(timezone.utc)
    table = _get_table()

    saved = 0
    duplicates = {}   # EmailID -> 直近1時間の出現件数

    for s3_record in event["Records"]:
        bucket = s3_record["s3"]["bucket"]["name"]
        key = unquote_plus(s3_record["s3"]["object"]["key"])

        # 同じバケットに他のログソースも入るため、対象外は読まずに捨てる。
        # _extract_log_records はジェネレータなので、ここで抜ければ GetObject も走らない。
        if not _matches_target_prefix(key):
            print(f"Skip out-of-scope object: {key}")
            continue

        for log_record in _extract_log_records(bucket, key):
            item = _build_item(bucket, key, log_record, now)

            # 先に保存してから数える。ConsistentRead=True により、
            # 自分が今書いたレコードも、他の Lambda が先に書いたレコードも必ず数に入る。
            table.put_item(Item=item)
            saved += 1

            count = _count_recent(table, item["EmailID"], now)
            if count >= 2:
                email_id = item["EmailID"]
                duplicates[email_id] = max(duplicates.get(email_id, 0), count)
                print(f"Duplicate detected: EmailID={email_id} count={count}")

    notified = False
    if duplicates:
        notified = _notify_duplicates(duplicates, now)

    return {
        "saved_successful_send_logs": saved,
        "duplicate_email_ids": len(duplicates),
        "duplicate_notifications": int(notified),
    }


def _matches_target_prefix(key):
    """Return True when the object key falls under the configured prefix.

    オブジェクトキーが設定されたプレフィックス配下にあるかを返す。
    """
    # s3_key_format が "/service=..." とスラッシュ始まりのため、実際の S3 キーに
    # 先頭スラッシュが付くかどうかが環境で変わりうる。両側を正規化してから比較する。
    # 単純な startswith にすると、スラッシュの有無がずれた瞬間に全オブジェクトが
    # スキップされ、エラーも出ないまま検知が止まる。
    if not TARGET_KEY_PREFIX:
        return True
    return key.lstrip("/").startswith(TARGET_KEY_PREFIX.lstrip("/"))


def _extract_log_records(bucket, key):
    """Yield the fields of every successful mail-send log line in one S3 object.

    1つの S3 オブジェクト内の、メール送信成功ログ各行の保存用フィールドを返す。
    """
    # 展開しながら 1 行ずつ返すので、ピークメモリが行サイズで済む。
    body = S3.get_object(Bucket=bucket, Key=key)["Body"]
    stream = gzip.GzipFile(fileobj=body) if key.endswith(".gz") else body.iter_lines()

    # 行番号はマッチした行だけでなく「ファイル全体の行番号」を数える。
    # DynamoDB のレコードから原本の該当行へそのまま辿れるようにするため。
    #   aws s3 cp s3://<bucket>/<sourceKey> - | gunzip | sed -n '<sourceLineNumber>p'
    for line_number, raw_line in enumerate(stream, start=1):
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        parsed, log_line = _unwrap_json_log(line)

        match = SUCCESSFUL_SEND.match(log_line)
        if not match:
            continue

        print("Matched successful mail send:", json.dumps(
            {"EmailID": match["email_id"], "command": match["command"],
             "key": key, "lineNumber": line_number}))

        yield {
            "EmailID": match["email_id"],
            "command": match["command"],
            "sourceLineNumber": line_number,
            "rawLog": line,
            # 元ログの送信時刻。Fluent Bit の date と Laravel 行頭の両方から拾う。
            # 現仕様では判定に使わず保存のみ（判定基準は検知時刻に統一）。
            "logTimestamp": (parsed or {}).get("date") or match["log_timestamp"],
        }


def _unwrap_json_log(line):
    """Return parsed Fluent Bit fields and the Laravel message, or the raw line.

    Fluent Bit JSON Lines の項目と Laravel メッセージを返し、それ以外は元の行を返す。
    """
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None, line
    if isinstance(parsed, dict) and isinstance(parsed.get("log"), str):
        return parsed, parsed["log"]
    return parsed if isinstance(parsed, dict) else None, line


def _build_item(bucket, key, log_record, created_at):
    """Build one append-only record whose sort key is unique per log line.

    ソートキーがログ1行ごとに一意となる、追記型のレコードを1件作る。
    """
    # isoformat() の既定 timespec="auto" はマイクロ秒が 0 のとき小数部ごと落ちて
    # 幅が変わる。SK は文字列の辞書順で範囲比較されるため、必ず固定幅にする。
    # UTC 固定なのも同じ理由（オフセットが混在すると辞書順と時系列順がずれる）。
    created = created_at.isoformat(timespec="microseconds")

    # SK の一意性は 3 段構え。
    #   createdAt        … 実行ごとに変わる。先頭に置くことで時刻範囲検索が効く
    #   sourceKey        … $UUID を含むのでオブジェクトごとに必ず異なる
    #   sourceLineNumber … 同一実行・同一ファイル内の別行を区別する
    # 特に 3 つ目が無いと、同じファイルに同じ EmailID が 2 行あった場合に
    # PK+SK が完全一致して 2 件目が 1 件目を上書きし、検知できなくなる。
    record_key = f"{created}#{key}#{log_record['sourceLineNumber']}"

    item = {
        "EmailID": log_record["EmailID"],
        "recordKey": record_key,
        "createdAt": created,
        "command": log_record["command"],
        "sourceBucket": bucket,
        "sourceKey": key,
        "sourceLineNumber": log_record["sourceLineNumber"],
        "rawLog": log_record["rawLog"],
        "expiresAt": int((created_at + WINDOW).timestamp()),
    }
    if log_record["logTimestamp"]:
        item["logTimestamp"] = log_record["logTimestamp"]
    return item


def _count_recent(table, email_id, now):
    """Count records for this EmailID inside the rolling window.

    直近1時間に含まれる、この EmailID のレコード件数を数える。
    """
    # SK の先頭が createdAt なので、文字列の範囲比較がそのまま時刻の絞り込みになる。
    # 末尾の "#" は境界の意図を明示するための区切り。
    cutoff = (now - WINDOW).isoformat(timespec="microseconds") + "#"

    # ConsistentRead=True はベーステーブルの主キー経由でのみ指定できる。
    # PK を EmailID にしているからこそ使えて、書き込み直後のレコードが必ず数に入る。
    # Select="COUNT" は件数だけを返すので本文の転送が発生しない。
    kwargs = {
        "KeyConditionExpression": Key("EmailID").eq(email_id) & Key("recordKey").gte(cutoff),
        "ConsistentRead": True,
        "Select": "COUNT",
    }

    # Query は 1 回で最大 1MB 分しか評価せず、超過分はエラーではなく
    # LastEvaluatedKey として持ち越される。暴走ループで単一 EmailID の
    # レコードが大量になったとき件数が過少になるため、全ページ読み切る。
    total = 0
    while True:
        response = table.query(**kwargs)
        total += response["Count"]
        start_key = response.get("LastEvaluatedKey")
        if not start_key:
            return total
        kwargs["ExclusiveStartKey"] = start_key


def _get_table():
    """Return the table, bootstrapping it only when running against LocalStack.

    テーブルを返す。LocalStack 向けのときだけ自動作成する。
    """
    # 本番のテーブルは CloudShell から手動で作成する（構築手順は設計書を参照）。
    # ここで作成を試みないのは 2 つの理由による。
    #   1. 実行のたびに create_table と describe_time_to_live が飛び、
    #      ResourceInUseException を受け取るだけの無駄なコールになる
    #   2. Lambda の実行ロールに dynamodb:CreateTable と
    #      dynamodb:UpdateTimeToLive という、本来不要な権限が必要になる
    # 本番で必要な権限は dynamodb:PutItem と dynamodb:Query の 2 つだけで済む。
    if not os.environ.get("DYNAMODB_ENDPOINT"):
        return DYNAMODB.Table(TABLE_NAME)
    return _bootstrap_local_table()


def _bootstrap_local_table():
    """Create the composite-key table for local development only.

    ローカル開発用にのみ、複合キーのテーブルを作成する。
    """
    # LocalStack / DynamoDB Local 専用。実 DynamoDB では create_table が
    # ACTIVE 前に返るため、直後の put_item が失敗し得る点にも注意。
    try:
        table = DYNAMODB.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "EmailID", "KeyType": "HASH"},
                {"AttributeName": "recordKey", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "EmailID", "AttributeType": "S"},
                {"AttributeName": "recordKey", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
    except DYNAMODB.meta.client.exceptions.ResourceInUseException:
        return DYNAMODB.Table(TABLE_NAME)

    # TTL はストレージ掃除のみを担う。削除は非同期で最大 48 時間遅れることがあるため、
    # 1 時間の判定は TTL ではなく _count_recent の時刻条件で行う。
    client = DYNAMODB.meta.client
    status = client.describe_time_to_live(TableName=TABLE_NAME)["TimeToLiveDescription"]
    if status.get("TimeToLiveStatus") != "ENABLED":
        client.update_time_to_live(
            TableName=TABLE_NAME,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": "expiresAt"},
        )
    return table


def _notify_duplicates(duplicates, now):
    """Notify Slack with the duplicate scale only; IDs are not enumerated.

    個別 EmailID を列挙せず、重複の規模だけを Slack へ通知する。
    """
    # 通知本文の長さを重複件数によらず一定にするため、個別 EmailID は列挙しない。
    # 「最多の EmailID」の件数は _count_recent の戻り値そのもので、
    # 直近1時間のローリング件数を表す（集計のための追加検索はしない）。
    top_id, top_count = max(duplicates.items(), key=lambda item: item[1])
    message = (
        "⚠️ メール重複を検知しました\n"
        f"検知した重複: {len(duplicates)} 件\n"
        f"最多の EmailID: {top_count} 件（EmailID: {top_id}）\n"
        f"検知日時: {now.astimezone(DISPLAY_TZ):%Y-%m-%d %H:%M:%S} JST"
    )
    return _post_to_slack(message)


def _post_to_slack(message):
    """Post a notification. Returns False on failure without raising.

    通知を送信する。失敗時は例外を送出せず False を返す。
    """
    print(message)

    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        print("SLACK_WEBHOOK_URL is not set; skip Slack notification.")
        return False

    webhook_request = request.Request(
        webhook_url,
        data=json.dumps({"text": message}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with request.urlopen(webhook_request, timeout=5) as response:
            print(f"Slack notification sent: HTTP {response.status}")
            return True
    except error.URLError as exc:
        # Lambda リトライを行わない方針のため、例外は再送出せず False を返す。
        # ただし握り潰すとアラートが無言で消えるので、固定文字列を出力して
        # CloudWatch Logs のメトリクスフィルタ + アラームで拾えるようにする。
        # フィルタパターン: "MAIL_DUPLICATE_SLACK_FAILED"
        print(f"MAIL_DUPLICATE_SLACK_FAILED Slack notification failed: {exc}")
        return False
