#!/usr/bin/env python3
"""Offline tests for the mail duplicate detector: no AWS, no LocalStack, no Docker.

AWS もコンテナも使わずに検知ロジックを検証する。boto3 をメモリ上の代替へ差し替え、
detect_mail_duplicates.py をそのまま読み込んで handler を呼ぶ。

  python3 scripts/test-mail-duplicate-offline.py

シェルの検証スクリプトとの役割分担:
  ここ                      判定ロジックそのもの。速く、網羅的
  test-mail-duplicate-*.sh  S3 通知・並行実行・設定変更など、代替では見えない部分

レコード数ではなくキー単位で数えていること（ケース11）は、ここでしか測っていない。
行ごとに数える実装へ戻ると、同一キーが N 行あるとき読み取り量が N の二乗に膨らむ。
"""

import importlib.util
import io
import json
import os
import sys
import types
from pathlib import Path

DETECTOR = Path(__file__).resolve().parents[1] / "localstack/init/ready.d/detect_mail_duplicates.py"


# --------------------------------------------------------------------------
# boto3 の代替
# --------------------------------------------------------------------------
class _Cond:
    def __init__(self, name=None, op=None, value=None):
        self.parts = [] if name is None else [(name, op, value)]
    def __and__(self, other):
        c = _Cond(); c.parts = self.parts + other.parts; return c


class Key:
    def __init__(self, name): self.name = name
    def eq(self, v): return _Cond(self.name, "eq", v)
    def gte(self, v): return _Cond(self.name, "gte", v)


class FakeTable:
    """The partition key is whatever the caller actually keys on, like DynamoDB."""
    def __init__(self, name, key_name=None):
        self.name, self.key_name = name, key_name
        self.items = {}
        self.query_calls = 0

    def _key_of(self, item):
        if self.key_name is None:
            # 実テーブルと同じく、キー属性が無ければ書き込みは失敗させる。
            for candidate in ("EmailID", "mail_to_content_hash"):
                if candidate in item:
                    self.key_name = candidate
                    break
            else:
                raise KeyError("no partition key attribute in item")
        return item[self.key_name]

    def put_item(self, Item):
        if self.key_name is not None and self.key_name not in Item:
            raise KeyError(f"item is missing the partition key {self.key_name}")
        self.items[(self._key_of(Item), Item["recordKey"])] = dict(Item)

    def query(self, **kwargs):
        self.query_calls += 1
        pk = cutoff = name_of_pk = None
        for name, op, value in kwargs["KeyConditionExpression"].parts:
            if op == "eq": pk, name_of_pk = value, name
            else: cutoff = value
        rows = [(sk, i) for (k, sk), i in self.items.items()
                if k == pk and i.get(name_of_pk) == pk and (cutoff is None or sk >= cutoff)]
        # 実テーブルと同じく、ソートキー順に返し、Limit はその後に効かせる。
        rows.sort(key=lambda pair: pair[0], reverse=not kwargs.get("ScanIndexForward", True))
        items = [i for _, i in rows]
        limit = kwargs.get("Limit")
        if limit is not None:
            items = items[:limit]
        return {"Count": len(items), "Items": items}

    def wait_until_exists(self): pass


class FakeDynamo:
    def __init__(self):
        self.tables = {}
        self.meta = types.SimpleNamespace(
            client=types.SimpleNamespace(
                exceptions=types.SimpleNamespace(ResourceInUseException=type("E", (Exception,), {})),
                describe_time_to_live=lambda TableName: {"TimeToLiveDescription": {"TimeToLiveStatus": "ENABLED"}},
                update_time_to_live=lambda **kw: None,
            )
        )

    def Table(self, name):
        return self.tables.setdefault(name, FakeTable(name))

    def create_table(self, TableName, KeySchema, **kw):
        key_name = next(k["AttributeName"] for k in KeySchema if k["KeyType"] == "HASH")
        self._keys[TableName] = key_name
        return self.tables.setdefault(TableName, FakeTable(TableName, key_name))

    _keys = {}


class FakeS3:
    def __init__(self): self.objects = {}
    def get_object(self, Bucket, Key):
        body = self.objects[(Bucket, Key)]
        stream = io.BytesIO(body.encode("utf-8"))
        stream.iter_lines = lambda: iter(stream.read().splitlines())
        return {"Body": stream}


S3, DDB = FakeS3(), FakeDynamo()


def _install_fake_boto3():
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda service, **kw: S3
    boto3.resource = lambda service, **kw: DDB
    conditions = types.ModuleType("boto3.dynamodb.conditions"); conditions.Key = Key
    dynamodb = types.ModuleType("boto3.dynamodb"); dynamodb.conditions = conditions
    boto3.dynamodb = dynamodb
    sys.modules.update({"boto3": boto3, "boto3.dynamodb": dynamodb,
                        "boto3.dynamodb.conditions": conditions})
    return S3, DDB


# --------------------------------------------------------------------------
# テスト本体
# --------------------------------------------------------------------------
S3, DDB = _install_fake_boto3()

PASS = FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  PASS  {name}")
    else:    FAIL += 1; print(f"  FAIL  {name} {extra}")

def load(**env):
    """Reload the detector with a fresh environment and empty tables."""
    for k in ("TABLE_NAME","CONTENT_TABLE_NAME","WINDOW_MINUTES","SUPPRESS_SUBJECT_HASHES",
              "NOTIFY_DISABLED","NOTIFY_DISABLED_CONTENT","TARGET_KEY_PREFIX","SLACK_WEBHOOK_URL"):
        os.environ.pop(k, None)
    os.environ.update(env)
    S3.objects.clear(); DDB.tables.clear()
    spec = importlib.util.spec_from_file_location("detector", DETECTOR)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["detector"] = mod
    spec.loader.exec_module(mod)
    mod.sent = []
    mod._post_to_slack = lambda m: (mod.sent.append(m), True)[1]
    return mod

def line(email_id, *, content_hash="c" * 16, mail_to_hash="m" * 16,
         subject_hash="s" * 16, command="EvenSendEmails", context=True, broken=False):
    msg = f"[2026-09-25 01:02:03] production.INFO: {command} [production] success  sending email: {email_id}"
    if not context:
        return msg
    if broken:
        return msg + ' {"content_hash": '
    ctx = {"mail_object_type": "App\\\\User", "subject": "s",
           "content_hash": content_hash, "mail_to_hash": mail_to_hash,
           "subject_hash": subject_hash, "company": "acme.com",
           "language": "jp", "created_at": "2026-09-25 01:00:00"}
    return msg + " " + json.dumps(ctx)

def run(mod, lines, key="service=worker/env=production/log_source=laravel-app/a.log"):
    S3.objects[("bucket", key)] = "\n".join(lines) + "\n"
    return mod.handler({"Records": [{"s3": {"bucket": {"name": "bucket"},
                                            "object": {"key": key}}}]}, None)

print("\n=== 1. 同一 EmailID の再送 ===")
m = load(); r = run(m, [line("100"), line("100")])
check("EmailID 重複を検知", r["duplicate_email_ids"] == 1, r)
check("通知は1通", len(m.sent) == 1)

print("\n=== 2. 別 EmailID・同じ宛先と本文 ===")
m = load(); r = run(m, [line("100"), line("101")])
check("本文重複を検知", r["duplicate_content_keys"] == 1, r)
check("EmailID では検知しない", r["duplicate_email_ids"] == 0, r)
check("本文レコードを2件保存", r["saved_content_records"] == 2, r)

print("\n=== 3. 本文が違えば鳴らない ===")
m = load(); r = run(m, [line("100"), line("101", content_hash="d"*16)])
check("重複なし", r["duplicate_content_keys"] == 0 and r["duplicate_email_ids"] == 0, r)
check("通知なし", len(m.sent) == 0)

print("\n=== 4. 宛先が違えば鳴らない（一斉配信） ===")
m = load(); r = run(m, [line("100"), line("101", mail_to_hash="n"*16)])
check("重複なし", r["duplicate_content_keys"] == 0, r)

print("\n=== 5. ハッシュの無い旧ログ ===")
m = load(); r = run(m, [line("100", context=False), line("100", context=False)])
check("本文テーブルへ保存しない", r["saved_content_records"] == 0, r)
check("スキップ件数を数える", r["skipped_without_hash"] == 2, r)
check("EmailID 検知は動く", r["duplicate_email_ids"] == 1, r)

print("\n=== 6. context JSON が壊れている ===")
m = load(); r = run(m, [line("100", broken=True), line("101", broken=True)])
check("本文テーブルへ保存しない", r["saved_content_records"] == 0, r)
check("落ちずに処理を続ける", r["saved_successful_send_logs"] == 2, r)

print("\n=== 7. 件名による通知抑止 ===")
m = load(SUPPRESS_SUBJECT_HASHES="s"*16); r = run(m, [line("100"), line("101")])
check("検知はする", r["duplicate_content_keys"] == 1, r)
check("Slack へ送らない", len(m.sent) == 0)

print("\n=== 8. 本文重複だけ通知停止 ===")
m = load(NOTIFY_DISABLED_CONTENT="2026-09-25 調査中")
r = run(m, [line("100"), line("100"), line("101")])
check("EmailID 重複は通知する", len(m.sent) == 1, m.sent)
check("本文重複は本文に出ない", "同じ宛先へ同じ内容" not in (m.sent[0] if m.sent else ""), m.sent)

print("\n=== 9. 全通知停止 ===")
m = load(NOTIFY_DISABLED="止めた"); r = run(m, [line("100"), line("100")])
check("検知はする", r["duplicate_email_ids"] == 1, r)
check("Slack へ送らない", len(m.sent) == 0)

print("\n=== 10. 判定窓を狭められる ===")
m = load(WINDOW_MINUTES="30")
check("WINDOW=30分", m.WINDOW.total_seconds() == 1800, m.WINDOW)
m = load(WINDOW_MINUTES="abc")
check("不正値は60分へ", m.WINDOW.total_seconds() == 3600, m.WINDOW)
m = load(WINDOW_MINUTES="2000")
check("範囲外は60分へ", m.WINDOW.total_seconds() == 3600, m.WINDOW)

print("\n=== 11. カウントは行ごとではなくキーごと ===")
m = load(); run(m, [line("100") for _ in range(10)])
t = DDB.tables["mail_send_log_events"]
check("10行でも Query は1回", t.query_calls == 1, f"query_calls={t.query_calls}")

print("\n=== 12. 保存レコードの中身 ===")
m = load(); run(m, [line("100")])
ct = DDB.tables["mail_send_log_events_by_content"]
rec = list(ct.items.values())[0]
check("PK は <mail_to>:<content>", rec["mail_to_content_hash"] == "m"*16 + ":" + "c"*16, rec.get("mail_to_content_hash"))
check("EmailID は通常属性", rec.get("emailId") == "100", rec.get("emailId"))
check("EmailID はキーにしない", "EmailID" not in rec)
check("subjectHash を持つ", rec.get("subjectHash") == "s"*16)
check("company を持つ", rec.get("company") == "acme.com")
check("appCreatedAt は createdAt と別物", rec.get("appCreatedAt") == "2026-09-25 01:00:00" and rec["createdAt"] != rec["appCreatedAt"])

print("\n=== 13. 通知に EmailID を最大3件まで載せる ===")
m = load(); run(m, [line("100"), line("101")])
msg = m.sent[0] if m.sent else ""
check("2件なら両方載る", "EmailID: 100, 101" in msg, msg)
check("省略記号は付かない", "..." not in msg, msg)

m = load(); run(m, [line(str(i)) for i in range(200, 205)])
msg = m.sent[0] if m.sent else ""
check("5件でも3件だけ載る", "EmailID: 200, 201, 202, ..." in msg, msg)
check("件数は省略せず出る", "最多: 5 通" in msg, msg)

m = load(); run(m, [line("300"), line("301")])
ct = DDB.tables["mail_send_log_events_by_content"]
check("EmailID の取得は1回だけ", ct.query_calls == 2, f"query_calls={ct.query_calls}")

print("\n=== 14. 対象外プレフィックスは読まない ===")
m = load(TARGET_KEY_PREFIX="service=worker/env=production/")
r = run(m, [line("100")], key="service=api/env=production/other.log")
check("何も保存しない", r["saved_successful_send_logs"] == 0, r)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
