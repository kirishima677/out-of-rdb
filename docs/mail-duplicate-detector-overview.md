# OAメール送信側重複検知: 現状整理

## 目的

メール送信成功ログに同じ `EmailID` が短時間に複数回現れたとき、送信側の重複を検知する。ローカル環境で AWS に近い配送経路と通知までを再現し、STG 導入前に仕様と実装を検証できる状態にする。

現時点の方針は、見逃しより通知過多を優先すること。1時間内に同じ `EmailID` がさらに検出されれば、追加でも通知する。

## 構成

```text
ログ（Fluent Bit JSON Lines / 平文 / gzip）
  → S3: mail-send-logs
  → S3 ObjectCreated イベント
  → Lambda: detect-mail-duplicates
       ├→ DynamoDB Local: mail_duplicate_events
       └→ 疑似 Slack API（ローカル）／Slack Incoming Webhook（STG・本番）
```

ローカルの疑似 Slack API も LocalStack 内に構築している。

```text
API Gateway → Lambda: mock-slack-api → DynamoDB Local: mock_slack_api_calls
```

## 実装済みの仕様

| 項目 | 現在の挙動 |
| --- | --- |
| 対象コマンド | `EvenSendEmails`、`OddSendEmails`、`SendEmails` |
| 対象ログ | `success sending email: <数字のEmailID>` |
| 入力形式 | 平文、Fluent Bit JSON Lines、`.gz` |
| 重複判定 | Lambda が検知した時刻から直近1時間に同じ `EmailID` が2件以上 |
| 履歴 | `mail_duplicate_events` に `EmailID`、`createdAt`、S3入力元などを保存 |
| 通知 | `SLACK_WEBHOOK_URL` があれば JSON の `{"text":"..."}` を POST。未設定なら Lambda ログのみ |
| ローカル通知先 | 起動時に `always-success` の疑似 Slack URL を環境変数へ自動設定 |
| Slack エラー | ログへ記録するが、検知 Lambda 自体は失敗にしない |

## 主なファイル

| 目的 | ファイル |
| --- | --- |
| 重複検知 Lambda | `localstack/init/ready.d/detect_mail_duplicates.py` |
| 疑似 Slack API Lambda | `localstack/init/ready.d/mock_slack_api.py` |
| LocalStack の自動構築 | `localstack/init/ready.d/01-bootstrap-fluentbit-sample.sh` |
| DynamoDB 履歴表示 | `client/sample/show_mail_duplicate_events.py` |
| 通常の検知テスト | `scripts/test-mail-duplicate-detector.sh` |
| 100件性能テスト | `scripts/benchmark-mail-duplicate-detector.sh` |
| 近接二重配送テスト | `scripts/test-mail-duplicate-concurrent.sh` |

## テストパターン

| パターン | 実行 | 確認内容 | 後片付け |
| --- | --- | --- | --- |
| 通常の重複検知 | `./scripts/test-mail-duplicate-detector.sh` | 同じ EmailID 2件、検知ログ、DynamoDB 2件、疑似 Slack 1回 | 自動削除 |
| 検知履歴の目視 | `KEEP_TEST_DATA=1 ./scripts/test-mail-duplicate-detector.sh` | 検知直後の DynamoDB レコード | 手動削除 |
| 履歴表示 | `python /workspace/sample/show_mail_duplicate_events.py` | 全件を新しい順に表示 | 読み取りのみ |
| 指定 ID の履歴表示 | `python /workspace/sample/show_mail_duplicate_events.py --email-id <ID>` | 指定 EmailID の履歴のみ | 読み取りのみ |
| 100件性能 | `./scripts/benchmark-mail-duplicate-detector.sh` | 100行の1 S3イベント、全件保存時間、Lambda Duration | 自動削除 |
| 性能件数変更 | `RECORD_COUNT=500 ./scripts/benchmark-mail-duplicate-detector.sh` | 任意件数の処理時間 | 自動削除 |
| 近接二重配送 | `./scripts/test-mail-duplicate-concurrent.sh` | 100 ID × 2配送 = 200件、各 ID が2件ずつ | 自動削除 |
| 近接二重配送の変更 | `RECORD_COUNT=200 DELAY_SECONDS=0.5 ./scripts/test-mail-duplicate-concurrent.sh` | 件数・配送間隔を変えた保存確認 | 自動削除 |
| 疑似 Slack 成功 | `POST /slack/always-success` | HTTP 200 | カウンターは `reset=true` で削除 |
| 疑似 Slack 継続失敗 | `POST /slack/always-failure` | HTTP 500 | 同上 |
| 疑似 Slack 復旧 | `POST /slack/fail-twice-then-success` | 500 → 500 → 200 | 同上 |

疑似 Slack API の URL 取得と呼び出し例は [mock-slack-api.md](mock-slack-api.md) を参照する。

## 実行前提

LocalStack の起動・更新後は、次を実行する。

```bash
docker compose up -d --force-recreate localstack
```

`client` コンテナで履歴表示をする場合は、初回だけ Python 仮想環境と依存関係を準備する。

```bash
docker exec -it client sh
python3 -m venv /workspace/.venv
. /workspace/.venv/bin/activate
pip install -r /workspace/requirements.txt
```

## 実測済みの結果

- 100ログ行を1つの S3 オブジェクトとして配送し、DynamoDB Local への全件保存と Lambda `REPORT` の処理時間を確認済み
- 同じ100件を0.5秒差で二重配送し、100 EmailID × 2件 = 200件が保存されることを確認済み
- 疑似 Slack API の `200`、`500`、`500 → 500 → 200` を確認済み
- 通常の重複検知テストで、疑似 Slack が1回呼び出されることを確認済み

ローカルの処理時間は AWS 本番のネットワーク遅延や Lambda コールドスタートを表すものではなく、ロジックと概算性能の確認値として扱う。

## 未実装・次段階の候補

| 項目 | 理由 |
| --- | --- |
| 通知用 SQS と通知 Lambda の分離 | Slack 障害で検知処理を巻き込まないため |
| 通知のリトライと通知 DLQ | Slack の一時障害／継続障害を再処理できるようにするため |
| DynamoDB 書き込み失敗用の検知 DLQ | 検知履歴そのものを失わず、元イベントから再処理するため |
| イベント冪等化 | S3 重複配送・Lambda 再試行で同じログを重複保存しない必要が出た場合のため |
| 通知の抑制・冪等化 | 通知過多が運用上の問題になった場合のため |
| STG の Slack Webhook 接続 | 実チャンネルへの到達、本文、失敗時の見え方を確認するため |

DLQ は単なる保管場所である。再実行にはリドライブまたは再処理 Lambda を別途設計する。

## 段階導入の目安

1. 現在の構成を STG へ小さく導入し、実ログ量と通知内容を確認する。
2. STG の Slack Incoming Webhook を `SLACK_WEBHOOK_URL` として設定する。
3. Slack 障害への対応が必要になった時点で、通知を SQS + 通知 Lambda + 通知 DLQ へ分離する。
4. DynamoDB 失敗や再実行の要件が明確になった時点で、検知 DLQ と冪等化を追加する。
