# OAメール送信側重複検知 Lambda

`mail-send-logs` バケットに保存されたメール送信ログを `detect-mail-duplicates` Lambda が処理するサンプルです。

## 判定仕様

- 対象コマンドは `EvenSendEmails`、`OddSendEmails`、`SendEmails`
- `success sending email: <EmailID>` を含むログ行だけを対象にする
- `failed sending email:` を含む失敗ログは正規表現に一致しないため対象外
- `EmailID` ごとに、Lambda が検知した現在時刻を `createdAt` として DynamoDB Local の `mail_duplicate_events` テーブルへ記録する
- `createdAt` が直近1時間の同一 `EmailID` を強整合性読み取りで数え、2件以上で Lambda ログへ通知内容を出す
- 同じS3オブジェクト内に同じ `EmailID` が3件以上あっても、Lambdaの1回の実行につき通知は1回だけ

`SLACK_WEBHOOK_URL` が設定されている場合、`_notify_duplicate` は Lambda ログへ次の形式で出力した後、Slack Incoming Webhook へ同じ通知を POST します。未設定の場合は Lambda ログ出力だけで処理を続けます。

```text
⚠️ メール重複を検知しました EmailID: 18500638 / 件数: 2 / 検知日時(UTC): ...
```

ローカル環境では、起動スクリプトが `SLACK_WEBHOOK_URL` に LocalStack 疑似 Slack API の `always-success` URL を自動設定します。AWS では同じ環境変数へ実際の Webhook URL を渡します。Webhook URL はソースコードへ書かず、Secrets Manager などから渡します。

## 入力形式

平文ログと Fluent Bit 形式の JSON Lines の両方を処理します。S3キーが `.gz` で終わる場合は gzip 展開してから読み込みます。

実ログで確認した入力例:

```json
{"log":"[2026-08-21 07:11:11] production.INFO: EvenSendEmails [production] success  sending email: 18500638 {...}"}
```

## 起動と確認

```bash
docker compose up -d --force-recreate localstack

# Lambda と S3通知設定の確認
docker exec localstack awslocal lambda get-function --function-name detect-mail-duplicates
docker exec localstack awslocal s3api get-bucket-notification-configuration --bucket mail-send-logs
```

検出を試すには、同一 `EmailID` を含む2つのログファイルを `mail-send-logs` へ順に置きます。

```bash
docker exec localstack sh -lc '
printf "%s\\n" "[2026-09-13 10:00:00] production.INFO: SendEmails [production] success  sending email: 900001" > /tmp/mail-1.log
printf "%s\\n" "[2026-09-13 10:01:00] production.INFO: SendEmails [production] success  sending email: 900001" > /tmp/mail-2.log
awslocal s3 cp /tmp/mail-1.log s3://mail-send-logs/test/mail-1.log
awslocal s3 cp /tmp/mail-2.log s3://mail-send-logs/test/mail-2.log
'
```

> 実運用ログの `EmailID` は数字のため、検知スクリプトは数字のみを抽出します。

Lambdaログと保存結果を確認します。

```bash
docker exec localstack awslocal logs filter-log-events \
  --log-group-name /aws/lambda/detect-mail-duplicates

docker exec localstack awslocal dynamodb query \
  --endpoint-url http://dynamodb:8000 \
  --table-name mail_duplicate_events \
  --key-condition-expression 'EmailID = :email_id' \
  --expression-attribute-values '{":email_id":{"S":"900001"}}' \
  --consistent-read
```

より見やすくローカルの履歴を表示するには、`client` コンテナ内で次を実行します。

```bash
python /workspace/sample/show_mail_duplicate_events.py

# 特定の EmailID だけを確認する場合
python /workspace/sample/show_mail_duplicate_events.py --email-id 900001
```

## 自動動作確認

S3 配送、Lambda の重複検知ログ、DynamoDB Local への2件の保存をまとめて確認するシェルです。

```bash
./scripts/test-mail-duplicate-detector.sh
```

実行ごとに一意な数値 EmailID を使用します。成功・失敗を問わず終了時に、このシェルが作った S3 オブジェクトと DynamoDB レコードだけを削除するため、既存の検知履歴は変更しません。

検証直後のレコードを閲覧したい場合だけは、次のように `KEEP_TEST_DATA=1` を付けます。表示された EmailID を使って確認後、不要になったテストレコードは削除してください。

```bash
KEEP_TEST_DATA=1 ./scripts/test-mail-duplicate-detector.sh
python /workspace/sample/show_mail_duplicate_events.py --email-id '<表示された EmailID>'
```

## 処理時間のベンチマーク

Fluent Bit が複数ログ行をまとめて 1 件の S3 オブジェクトへ配送する想定で、既定100件の成功ログを処理します。S3 配送から DynamoDB Local へ全件が保存されるまでの時間と、Lambda の `REPORT` 行の `Duration` を表示します。

```bash
./scripts/benchmark-mail-duplicate-detector.sh

# 件数を変更する場合
RECORD_COUNT=500 ./scripts/benchmark-mail-duplicate-detector.sh
```

既定では終了時に、そのベンチマークで作成した S3 オブジェクトと DynamoDB レコードを削除します。結果を見たまま残す場合は `KEEP_TEST_DATA=1` を指定します。

## 近接した重複実行の検証

同じ100件の `EmailID` を含む2つの S3 オブジェクトを、既定で0.5秒差で配送します。2つの Lambda 実行が重なりうる状態で、各 `EmailID` がちょうど2件ずつ、合計200件 DynamoDB Local に保存されたことを確認します。

```bash
./scripts/test-mail-duplicate-concurrent.sh

# 件数と配送間隔を変更する場合
RECORD_COUNT=200 DELAY_SECONDS=0.5 ./scripts/test-mail-duplicate-concurrent.sh
```

このテストも既定では作成したデータだけを削除します。`KEEP_TEST_DATA=1` を付けると、実行後に DynamoDB の中身を確認できます。

## 意図的に対象外としていること

設計メモに合わせ、Fluent Bit の再送、S3イベントの重複配信、Lambda再試行、複数Lambdaの同時実行による競合は防止しません。これらは将来、S3オブジェクトのバージョンIDやETagを使ったイベント冪等化、DynamoDB条件付き書き込み、通知の冪等化で追加対応できます。
