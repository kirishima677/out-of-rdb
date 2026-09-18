# OAメール送信側重複検知 Lambda

`mail-send-logs` バケットに保存されたメール送信ログを `detect-mail-duplicates` Lambda が処理するサンプルです。

## 判定仕様

- 対象コマンドは `EvenSendEmails`、`OddSendEmails`、`SendEmails`
- `success sending email: <EmailID>` を含むログ行だけを対象にする
- `failed sending email:` を含む失敗ログは正規表現に一致しないため対象外
- ログ1行につき1レコードを DynamoDB Local の `mail_send_log_events` テーブルへ記録する。`EmailID` をパーティションキー、`{createdAt}#{sourceKey}#{行番号}` をソートキーとする複合キーで、同じ `EmailID` の出現が上書きされず別レコードとして積み上がる。`logTimestamp`、コマンド、S3入力元、生ログも属性として保持する
- `expiresAt` を DynamoDB TTL 属性として有効化し、検知時刻から1時間後を削除対象にする。TTL削除は非同期で最大48時間遅れるため、判定は TTL ではなく `createdAt` の時刻条件で行う
- `EmailID` を指定したベーステーブルの `Query`（`ConsistentRead=true`）で直近1時間の同一 ID を数え、2件以上で重複と判定する。**GSI は使わない**。GSI は仕様上 `ConsistentRead` を指定できず、書き込み直後のレコードが検索に載らないため
- Slack には個別 EmailID を全件列挙せず、今回の Lambda 実行で検知した重複件数と、`EmailID` あたりの最多件数を通知する

`SLACK_WEBHOOK_URL` が設定されている場合、`_notify_duplicates` は Lambda ログへ次の形式で出力した後、Slack Incoming Webhook へ同じ通知を POST します。未設定の場合は Lambda ログ出力だけで処理を続けます。

```text
⚠️ メール重複を検知しました
検知した重複: 1 件
最多の EmailID: 2 件（EmailID: 900001）
検知日時: 2026-09-13 19:01:00 JST
```

`最多の EmailID` の件数は判定に使った `Query` の件数そのもので、直近1時間のローリング値です。`2 件` で止まっていれば二重起動、`3`、`4` と伸びていれば同じ行を送り続けるループ、と通知だけで切り分けられます。

ローカル環境では、起動スクリプトが `SLACK_WEBHOOK_URL` に LocalStack 疑似 Slack API の `always-success` URL を自動設定します。AWS では同じ環境変数へ実際の Webhook URL を渡します。現状のコードは環境変数から直接読み取るため、Secrets Manager や SSM Parameter Store の SecureString へ移す場合は Lambda 側に取得処理の実装が必要です。

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
  --table-name mail_send_log_events \
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

## 同一ファイル内の重複の検証

1つの S3 オブジェクトに同じ `EmailID` を含む行が2つある場合を検証します。同一オブジェクト内の行は同じ Lambda 実行で処理されるため `createdAt` と `sourceKey` が同値になり、ソートキーに行番号が含まれていないと2件目が1件目を上書きして検知できなくなります。その回帰テストです。

```bash
./scripts/test-mail-duplicate-same-file.sh
```

件数の確認だけでなく、保存された2件が本当に `createdAt` と `sourceKey` を共有していたことも検証します。これがないと、たまたま時刻がずれた場合に衝突ケースを踏まないまま成功してしまいます。

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

設計メモに合わせ、Fluent Bit の再送、S3イベントの重複配信、Lambda再試行は防止しません。

**複数 Lambda の同時実行は対象です。** `EmailID` をパーティションキーとするベーステーブルへの強整合読み取りにより、同時に起動した複数の Lambda のいずれかが必ず重複を検知します。ただし双方が検知して通知が2回飛ぶことがあり（ローカル計測で5回中1回）、通知の重複排除は行っていません。

S3イベントの重複配信については、同一オブジェクトの再配信で同じログ行が別レコードとして保存され、そのオブジェクトに含まれる全 `EmailID` が重複と判定されます。対応する場合は、ソートキーから `createdAt` を外して `{sourceKey}#{行番号}` とし（再処理しても同一キーとなり上書きされる）、直近1時間の絞り込みを `createdAt` 属性での比較へ移します。
