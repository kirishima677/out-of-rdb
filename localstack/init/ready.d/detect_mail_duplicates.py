"""Detect duplicate mail sends from S3-stored send logs and notify Slack.

S3 に保存されたメール送信ログから同一 EmailID の重複送信を検知し、Slack へ通知する。

データモデル:
  ログ1行につき1レコードを保存する追記型。PK=EmailID / SK=recordKey の複合キーにより、
  同じ EmailID の出現が上書きされず別レコードとして積み上がる。
  これにより「1時間ローリングでの判定」と「何件目か」が同時に取れる。

  PK が EmailID なのでベーステーブルを直接 Query でき、ConsistentRead=True を指定できる。
  GSI は仕様上 ConsistentRead を指定できず常に結果整合性のため、判定経路には使わない。
  アプリサーバーが 2〜3 台あり Lambda が同時起動する環境では、これが検知漏れの分かれ目になる。

  検知は 2 種類ある。判定ロジックは共通で、パーティションキーだけが違う。
    1. 同一 EmailID の再送        PK = EmailID
    2. 同じ宛先へ同じ本文         PK = "<mail_to_hash>:<content_hash>"
  2 はアプリがログへ出すハッシュに依存する。ハッシュの無い行では 2 だけを飛ばし、
  1 は従来どおり動かす。追加機能が既存の検知を巻き込まないようにするため。
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

# STGと本番が同一アカウント・同一リージョンにあるため、テーブル名を環境ごとに分ける。
# AWS 上では必ず環境変数で渡す（stg_mail_send_log_events / prd_mail_send_log_events）。
# 既定値はローカルサンドボックス専用の名前であり、AWS 上のどの環境にも存在しない。
# これは意図的である。環境変数が欠けた場合、沈黙して他環境のテーブルへ書き込むのではなく、
# ResourceNotFoundException で落ちて Errors アラームに乗るようにしている。
TABLE_NAME = os.environ.get("TABLE_NAME", "mail_send_log_events")

# 本文重複検知のテーブル。既定値の扱いは TABLE_NAME と同じ理由による。
CONTENT_TABLE_NAME = os.environ.get("CONTENT_TABLE_NAME", "mail_send_log_events_by_content")


def _window_minutes():
    """Return the rolling-window length in minutes, falling back to 60.

    判定窓の長さ（分）を返す。不正な値なら既定の 60 に落とす。
    """
    raw = os.environ.get("WINDOW_MINUTES", "").strip()
    if not raw:
        return 60
    try:
        value = int(raw)
    except ValueError:
        print(f"WINDOW_MINUTES is not a number ({raw!r}); falling back to 60.")
        return 60
    # 24 時間以上に広げると、日次の登録リマインドが全件誤検知になる（仕様の前提条件3）。
    if not 1 <= value < 1440:
        print(f"WINDOW_MINUTES out of range ({value}); falling back to 60.")
        return 60
    return value


# 通知に載せる EmailID の最大件数。超えた分は "..." で省く。
# 本文の長さを重複の規模によらず一定に保つため。
NOTIFY_EMAIL_ID_LIMIT = 3

# 判定窓。同じキーがこの範囲に 2 件以上あれば重複とする。
# 誤検知が多いときに狭められるよう環境変数で変更できる。既定は 1 時間。
WINDOW = timedelta(minutes=_window_minutes())

# 既知事象の通知抑止。ここに載る subject_hash は保存もカウントもするが Slack へ送らない。
SUPPRESS_SUBJECT_HASHES = frozenset(
    part.strip()
    for part in os.environ.get("SUPPRESS_SUBJECT_HASHES", "").split(",")
    if part.strip()
)

# 通知の緊急停止。空でなければ有効。値には理由と日付を入れる運用とする
# （例: NOTIFY_DISABLED_CONTENT="2026-09-25 頻度調査中"）。
# 保存とカウントは止めない。止まるのは Slack への送信だけ。
NOTIFY_DISABLED = bool(os.environ.get("NOTIFY_DISABLED", "").strip())
NOTIFY_DISABLED_CONTENT = bool(os.environ.get("NOTIFY_DISABLED_CONTENT", "").strip())

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
    table = _get_table(TABLE_NAME, "EmailID")
    content_table = _get_table(CONTENT_TABLE_NAME, "mail_to_content_hash")

    saved = 0
    content_saved = 0
    skipped_without_hash = 0
    seen_email_ids = set()
    seen_content_keys = {}   # 本文キー -> subject_hash（抑止判定に使う）

    for s3_record in event["Records"]:
        bucket = s3_record["s3"]["bucket"]["name"]
        key = unquote_plus(s3_record["s3"]["object"]["key"])

        # 同じバケットに他のログソースも入るため、対象外は読まずに捨てる。
        # _extract_log_records はジェネレータなので、ここで抜ければ GetObject も走らない。
        if not _matches_target_prefix(key):
            print(f"Skip out-of-scope object: {key}")
            continue

        for log_record in _extract_log_records(bucket, key):
            # 保存だけを行い、キーを控える。件数は全行を書き終えてから数える。
            # 行ごとに数えると、同じキーが N 行あるとき 1+2+...+N 件を評価することになり、
            # 検知したい暴走そのもので読み取り量が行数の二乗に膨らむ。
            table.put_item(Item=_build_item(bucket, key, log_record, now))
            saved += 1
            seen_email_ids.add(log_record["EmailID"])

            content_key = _content_key(log_record["context"])
            if content_key is None:
                # アプリ変更前の旧ログ、または context JSON が読めなかった行。
                # EmailID 側の保存は済んでいる。本文テーブルだけを飛ばす。
                skipped_without_hash += 1
                continue

            content_table.put_item(
                Item=_build_content_item(bucket, key, log_record, now, content_key)
            )
            content_saved += 1
            seen_content_keys.setdefault(
                content_key, (log_record["context"] or {}).get("subject_hash")
            )

    if skipped_without_hash:
        print(
            f"Skipped {skipped_without_hash} line(s) without usable hashes; "
            "content-duplicate detection needs the app-side change."
        )

    # 全行を保存し終えてから、キーごとに 1 回だけ数える。
    # ConsistentRead=True により、自分が書いた分も他の Lambda が並行して書いた分も必ず入る。
    duplicates = _count_duplicates(table, "EmailID", seen_email_ids, now)
    content_duplicates = _count_duplicates(
        content_table, "mail_to_content_hash", seen_content_keys, now
    )

    notified = _notify(
        duplicates, content_duplicates, seen_content_keys, now, content_table
    )

    return {
        "saved_successful_send_logs": saved,
        "saved_content_records": content_saved,
        "skipped_without_hash": skipped_without_hash,
        "duplicate_email_ids": len(duplicates),
        "duplicate_content_keys": len(content_duplicates),
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
            # ログを出力したインスタンス。Fluent Bit が付与する instance_id。
            # S3 キーにホスト識別子が含まれないため、この属性が無いと
            # 「別インスタンスでの二重起動」か「同一インスタンスでの二重送信」かを
            # 事後に切り分けられない。根本原因の調査で直接必要になる情報である。
            "sourceHost": ((parsed or {}).get("instance_id")
                           or (parsed or {}).get("hostname")
                           or (parsed or {}).get("host")),
            # メッセージ部の後ろに付く Laravel の context JSON。ハッシュはここに入る。
            # アプリ変更前のログには存在しないため None になりうる。
            "context": _parse_log_context(log_line, match.end()),
        }


def _parse_log_context(log_line, start_at):
    """Return the Laravel context JSON appended after the message, or None.

    メッセージ部の後ろに付く Laravel の context JSON を返す。読めなければ None。
    """
    # 検索の起点をマッチ末尾にするのは、メッセージ部に "{" が現れても拾わないため。
    start = log_line.find("{", start_at)
    if start < 0:
        return None
    try:
        # raw_decode は先頭の 1 値だけを読む。Monolog が context の後ろに extra を
        # 付ける形式でも、末尾に空白が残る形でも失敗しない。
        value, _ = json.JSONDecoder().raw_decode(log_line[start:])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _content_key(context):
    """Return the content-table partition key, or None when hashes are missing.

    本文テーブルのパーティションキーを返す。ハッシュが揃わなければ None。
    """
    if not context:
        return None
    mail_to_hash = context.get("mail_to_hash")
    content_hash = context.get("content_hash")
    # 片方でも欠けたら判定できない。空文字も同様に扱う。
    if not isinstance(mail_to_hash, str) or not mail_to_hash:
        return None
    if not isinstance(content_hash, str) or not content_hash:
        return None
    return f"{mail_to_hash}:{content_hash}"


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
    if log_record["sourceHost"]:
        item["sourceHost"] = log_record["sourceHost"]
    return item


def _build_content_item(bucket, key, log_record, created_at, content_key):
    """Build the content-table record for one log line.

    本文テーブル用のレコードを 1 件作る。
    """
    # ソートキー・TTL・原本への辿り方は EmailID 側とまったく同じでよいので作り直さない。
    # 違うのはパーティションキーだけである。
    item = _build_item(bucket, key, log_record, created_at)
    del item["EmailID"]
    item["mail_to_content_hash"] = content_key

    # EmailID はキーではなく通常属性として持つ。本文キーだけでは元のメールに戻れないため。
    item["emailId"] = log_record["EmailID"]

    context = log_record["context"] or {}
    for attribute, field in (
        ("subjectHash", "subject_hash"),   # 既知事象の通知抑止に使う
        ("company", "company"),            # 通知に影響範囲を出すため
        ("language", "language"),          # 同じ通知で本文ハッシュが違う理由の説明
        ("appCreatedAt", "created_at"),    # emails 行の作成時刻。createdAt(処理時刻)とは別物
    ):
        value = context.get(field)
        if isinstance(value, str) and value:
            item[attribute] = value
    return item


def _count_duplicates(table, key_name, keys, now):
    """Count each key once and return those at or above the threshold.

    キーごとに 1 回だけ数え、閾値に達したものを返す。
    """
    duplicates = {}
    for key_value in keys:
        count = _count_recent(table, key_name, key_value, now)
        if count >= 2:
            duplicates[key_value] = count
            print(f"Duplicate detected: {key_name}={key_value} count={count}")
    return duplicates


def _fetch_email_ids(table, key_name, key_value, now, limit):
    """Return up to `limit` + 1 emailId values for this key, oldest first.

    このキーの emailId を古い順に最大 limit + 1 件返す。
    """
    # limit + 1 件取るのは「これ以上あるか」を判断するため。
    # 全件読むと、暴走で数千件たまっているときに転送量が跳ねる。
    cutoff = (now - WINDOW).isoformat(timespec="microseconds") + "#"
    try:
        response = table.query(
            KeyConditionExpression=Key(key_name).eq(key_value) & Key("recordKey").gte(cutoff),
            ConsistentRead=True,
            # ソートキーの先頭が createdAt なので、昇順は「先に検知された順」になる。
            ScanIndexForward=True,
            Limit=limit + 1,
            ProjectionExpression="emailId",
        )
    except Exception as exc:
        # 通知を飾るための情報であり、取れなくても検知は成立する。
        # ここで落として通知そのものを失うほうが損失が大きい。
        print(f"Could not read emailIds for {key_value}: {exc}")
        return []
    return [item["emailId"] for item in response.get("Items", []) if "emailId" in item]


def _format_email_ids(email_ids, limit):
    """Join ids for the message, marking that more exist.

    通知用に並べる。上限を超える場合は省略されていることを示す。
    """
    if not email_ids:
        return ""
    shown = ", ".join(email_ids[:limit])
    return f"{shown}, ..." if len(email_ids) > limit else shown


def _count_recent(table, key_name, key_value, now):
    """Count records for this key inside the rolling window.

    判定窓に含まれる、このキーのレコード件数を数える。
    """
    # SK の先頭が createdAt なので、文字列の範囲比較がそのまま時刻の絞り込みになる。
    # 末尾の "#" は境界の意図を明示するための区切り。
    cutoff = (now - WINDOW).isoformat(timespec="microseconds") + "#"

    # ConsistentRead=True はベーステーブルの主キー経由でのみ指定できる。
    # 判定キーをそのまま PK にしているからこそ使えて、書き込み直後のレコードが必ず数に入る。
    # Select="COUNT" は件数だけを返すので本文の転送が発生しない。
    kwargs = {
        "KeyConditionExpression": Key(key_name).eq(key_value) & Key("recordKey").gte(cutoff),
        "ConsistentRead": True,
        "Select": "COUNT",
    }

    # Query は 1 回で最大 1MB 分しか評価せず、超過分はエラーではなく
    # LastEvaluatedKey として持ち越される。暴走ループで単一キーの
    # レコードが大量になったとき件数が過少になるため、全ページ読み切る。
    # Select="COUNT" でも読み取り容量は実アイテムを読む場合と同じである。
    # 減るのは転送量だけで、RCU は減らない。
    total = 0
    while True:
        response = table.query(**kwargs)
        total += response["Count"]
        start_key = response.get("LastEvaluatedKey")
        if not start_key:
            return total
        kwargs["ExclusiveStartKey"] = start_key


def _get_table(table_name, key_name):
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
        return DYNAMODB.Table(table_name)
    return _bootstrap_local_table(table_name, key_name)


def _bootstrap_local_table(table_name, key_name):
    """Create the composite-key table for local development only.

    ローカル開発用にのみ、複合キーのテーブルを作成する。
    """
    # LocalStack / DynamoDB Local 専用。実 DynamoDB では create_table が
    # ACTIVE 前に返るため、直後の put_item が失敗し得る点にも注意。
    try:
        table = DYNAMODB.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": key_name, "KeyType": "HASH"},
                {"AttributeName": "recordKey", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": key_name, "AttributeType": "S"},
                {"AttributeName": "recordKey", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
    except DYNAMODB.meta.client.exceptions.ResourceInUseException:
        return DYNAMODB.Table(table_name)

    # TTL はストレージ掃除のみを担う。削除は非同期で最大 48 時間遅れることがあるため、
    # 1 時間の判定は TTL ではなく _count_recent の時刻条件で行う。
    client = DYNAMODB.meta.client
    status = client.describe_time_to_live(TableName=table_name)["TimeToLiveDescription"]
    if status.get("TimeToLiveStatus") != "ENABLED":
        client.update_time_to_live(
            TableName=table_name,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": "expiresAt"},
        )
    return table


def _notify(duplicates, content_duplicates, subject_hashes, now, content_table):
    """Decide whether to notify, then send at most one message.

    通知するかを判断し、送るなら 1 回だけ送る。
    """
    # 停止しても保存とカウントは済んでいる。解除後に DynamoDB から遡って数えられる。
    if NOTIFY_DISABLED:
        if duplicates or content_duplicates:
            print(
                "MAIL_DUPLICATE_NOTIFY_DISABLED all "
                f"emailIdKeys={len(duplicates)} contentKeys={len(content_duplicates)}"
            )
        return False

    # 抑止リストの件名は保存・カウント・ログを通常どおり行い、Slack へ送る段階だけで落とす。
    # 除外ではなく抑止にしているのは、修正されたことを記録側で確認したいため。
    notifiable_content = {}
    for content_key, count in content_duplicates.items():
        subject_hash = subject_hashes.get(content_key)
        if subject_hash and subject_hash in SUPPRESS_SUBJECT_HASHES:
            print(
                f"MAIL_DUPLICATE_SUPPRESSED known-issue subject={subject_hash} count={count}"
            )
            continue
        notifiable_content[content_key] = count

    if NOTIFY_DISABLED_CONTENT and notifiable_content:
        # 本文重複だけを止める。実績のある EmailID 検知は道連れにしない。
        print(
            "MAIL_DUPLICATE_NOTIFY_DISABLED content "
            f"contentKeys={len(notifiable_content)}"
        )
        notifiable_content = {}

    if not duplicates and not notifiable_content:
        return False

    return _post_to_slack(
        _build_message(duplicates, notifiable_content, now, content_table)
    )


def _build_message(duplicates, content_duplicates, now, content_table):
    """Build one Slack message covering both kinds of duplicate.

    2 種類の重複をまとめた Slack 本文を 1 通ぶん作る。
    """
    # 本文の長さを件数によらず一定にするため、個別のキーは列挙しない。
    # 件数は _count_recent の戻り値そのもので、判定窓のローリング件数を表す。
    lines = ["⚠️ メール重複を検知しました"]

    if duplicates:
        top_id, top_count = max(duplicates.items(), key=lambda item: item[1])
        lines.append(f"[同一メールの再送] 検知した重複: {len(duplicates)} 件")
        lines.append(f"　最多の EmailID: {top_count} 件（EmailID: {top_id}）")

    if content_duplicates:
        top_key, top_count = max(content_duplicates.items(), key=lambda item: item[1])
        lines.append(f"[同じ宛先へ同じ内容] 検知した組み合わせ: {len(content_duplicates)} 件")

        # 最多のキーについてだけ EmailID を引く。調査の入口を1つ渡すのが目的で、
        # 全件を並べる必要はない。件数に関わらず問い合わせは 1 回に収まる。
        email_ids = _format_email_ids(
            _fetch_email_ids(
                content_table, "mail_to_content_hash", top_key, now, NOTIFY_EMAIL_ID_LIMIT
            ),
            NOTIFY_EMAIL_ID_LIMIT,
        )
        if email_ids:
            lines.append(f"　最多: {top_count} 通（EmailID: {email_ids}）")
        else:
            lines.append(f"　最多: {top_count} 通")

    lines.append(f"検知日時: {now.astimezone(DISPLAY_TZ):%Y-%m-%d %H:%M:%S} JST")
    return "\n".join(lines)


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
