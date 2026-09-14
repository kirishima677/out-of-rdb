# OAメール多重送信検知：構築手順

> 本番環境の構築手順。設計は「OAメール多重送信検知_送信側重複_最小修正版.md」を参照。
> 本番 AWS への操作はブラウザの CloudShell から行う前提で、すべて AWS CLI のコマンドで記述する。
> 手順を再現可能な形で残すことが目的であり、常時稼働の自動化は導入しない。

## 変数

以降のコマンドで使う値。CloudShell のセッションごとに設定する。

```bash
export AWS_REGION=ap-northeast-1
export ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
export TABLE_NAME=mail_send_log_events
export FUNCTION_NAME=detect-mail-duplicates
export ROLE_NAME=detect-mail-duplicates-role
export LOG_BUCKET=<メール送信ログが保存されているS3バケット名>
export KEY_PREFIX='service=official-alumni/env=production/log_source=laravel-app/'
export SLACK_WEBHOOK_URL=<Slack Incoming Webhook の URL>
export ALARM_TOPIC_ARN=<通知先の SNS トピック ARN>
```

`KEY_PREFIX` は実際の S3 キーと一致させること。先頭スラッシュの有無を必ず実物で確認する。

```bash
aws s3 ls "s3://${LOG_BUCKET}/" --recursive | head -3
```

---

## 1. DynamoDB テーブル

`EmailID` をパーティションキー、`recordKey` をソートキーとする複合キーのテーブルを作る。GSI は作らない。

```bash
aws dynamodb create-table --table-name "${TABLE_NAME}" --attribute-definitions AttributeName=EmailID,AttributeType=S AttributeName=recordKey,AttributeType=S --key-schema AttributeName=EmailID,KeyType=HASH AttributeName=recordKey,KeyType=RANGE --billing-mode PAY_PER_REQUEST
```

```bash
aws dynamodb wait table-exists --table-name "${TABLE_NAME}"
```

TTL を有効化する。TTL はストレージ掃除のみを担い、1時間の判定には使わない。

```bash
aws dynamodb update-time-to-live --table-name "${TABLE_NAME}" --time-to-live-specification "Enabled=true,AttributeName=expiresAt"
```

確認。

```bash
aws dynamodb describe-table --table-name "${TABLE_NAME}" --query 'Table.{Status:TableStatus,Keys:KeySchema,Billing:BillingModeSummary.BillingMode}' && aws dynamodb describe-time-to-live --table-name "${TABLE_NAME}"
```

**PITR（ポイントインタイムリカバリ）は有効化しない。** データの寿命が1時間の検知用ステートであり、バックアップに意味がないうえ課金だけが乗る。既定は無効なので操作は不要。

---

## 2. IAM ロール

Lambda の実行ロールを作る。テーブル作成はこの手順で手動実行するため、Lambda 側には `dynamodb:CreateTable` などの権限を与えない。

```bash
cat > /tmp/trust-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "lambda.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF
aws iam create-role --role-name "${ROLE_NAME}" --assume-role-policy-document file:///tmp/trust-policy.json
```

権限は必要最小限の4つに絞る。

```bash
cat > /tmp/lambda-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DetectionTableAccess",
      "Effect": "Allow",
      "Action": ["dynamodb:PutItem", "dynamodb:Query"],
      "Resource": "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${TABLE_NAME}"
    },
    {
      "Sid": "ReadMailSendLogs",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::${LOG_BUCKET}/${KEY_PREFIX}*"
    },
    {
      "Sid": "WriteOwnLogs",
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:log-group:/aws/lambda/${FUNCTION_NAME}*"
    }
  ]
}
EOF
aws iam put-role-policy --role-name "${ROLE_NAME}" --policy-name detect-mail-duplicates --policy-document file:///tmp/lambda-policy.json
```

S3 の権限をプレフィックス限定にしているのは、この Lambda が他のログソースを読めないようにするため。

---

## 3. Lambda 関数

`detect_mail_duplicates.py` をアップロードして関数を作る。CloudShell にファイルを配置してから実行する。

```bash
zip -j /tmp/detect-mail-duplicates.zip detect_mail_duplicates.py
```

```bash
aws lambda create-function --function-name "${FUNCTION_NAME}" --runtime python3.12 --handler detect_mail_duplicates.handler --role "arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}" --timeout 120 --memory-size 256 --zip-file fileb:///tmp/detect-mail-duplicates.zip --environment "Variables={TARGET_KEY_PREFIX=${KEY_PREFIX},SLACK_WEBHOOK_URL=${SLACK_WEBHOOK_URL}}"
```

```bash
aws lambda wait function-active-v2 --function-name "${FUNCTION_NAME}"
```

**タイムアウトを 120 秒にしている理由**。ローカル計測で1ログ行あたり約 4.2ms（DynamoDB への書き込み1回＋強整合読み取り1回）だった。通常時は1オブジェクト 800 行程度で約 3.4 秒に収まるが、Fluent Bit が滞留したあとの一括フラッシュで行数が跳ねる。2時間ぶんが溜まった場合に約 40 秒となるため、既定の 3 秒はもちろん 30 秒でも足りない。

コードを更新する場合は次のとおり。

```bash
zip -j /tmp/detect-mail-duplicates.zip detect_mail_duplicates.py && aws lambda update-function-code --function-name "${FUNCTION_NAME}" --zip-file fileb:///tmp/detect-mail-duplicates.zip
```

---

## 4. Lambda の再試行を無効化する

```bash
aws lambda put-function-event-invoke-config --function-name "${FUNCTION_NAME}" --maximum-retry-attempts 0
```

**この設定は必須である。** S3 からの呼び出しは非同期であり、既定では未処理の例外に対して最大2回再試行される。再試行では同じ S3 オブジェクトが再処理されるが、`recordKey` の先頭に含まれる `createdAt` は実行のたびに変わるため、同じログ行が別レコードとして保存され、**実際には送信されていない重複が検知される**。

代償として、一時エラーで落ちたオブジェクトのログは再処理されず検知漏れとなる。手順 6 の `Errors` アラームで検出する。

---

## 5. S3 イベント通知

まず S3 が Lambda を呼べるようにする。

```bash
aws lambda add-permission --function-name "${FUNCTION_NAME}" --statement-id s3-invoke --action lambda:InvokeFunction --principal s3.amazonaws.com --source-arn "arn:aws:s3:::${LOG_BUCKET}" --source-account "${ACCOUNT_ID}"
```

**通知設定を書く前に、既存の設定を必ず退避すること。** `put-bucket-notification-configuration` はバケットの通知設定を**全置換**するため、他の通知が設定されていると消える。

```bash
aws s3api get-bucket-notification-configuration --bucket "${LOG_BUCKET}" | tee /tmp/s3-notification-backup.json
```

出力が空（`{}`）でなければ、その内容に本設定を**追記する形**で編集すること。空の場合のみ、以下をそのまま使える。

```bash
cat > /tmp/s3-notification.json <<EOF
{
  "LambdaFunctionConfigurations": [
    {
      "Id": "detect-mail-duplicates",
      "LambdaFunctionArn": "arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}",
      "Events": ["s3:ObjectCreated:*"],
      "Filter": {
        "Key": {
          "FilterRules": [
            { "Name": "prefix", "Value": "${KEY_PREFIX}" }
          ]
        }
      }
    }
  ]
}
EOF
aws s3api put-bucket-notification-configuration --bucket "${LOG_BUCKET}" --notification-configuration file:///tmp/s3-notification.json
```

プレフィックスフィルタを設定するのは、同じバケットに入る他のログソース（nginx、php-fpm など）で Lambda を起動させないため。Lambda 側にも同じプレフィックスの判定があるが、そちらは設定漏れに備えた保険であり、起動自体は防げない。

---

## 6. CloudWatch アラーム

### 6-1. Lambda の失敗を検出する

検知パイプライン自体が停止しても、そのままでは「重複がない」状態と区別がつかない。

```bash
aws cloudwatch put-metric-alarm --alarm-name detect-mail-duplicates-errors --namespace AWS/Lambda --metric-name Errors --dimensions Name=FunctionName,Value="${FUNCTION_NAME}" --statistic Sum --period 300 --evaluation-periods 1 --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold --treat-missing-data notBreaching --alarm-actions "${ALARM_TOPIC_ARN}"
```

### 6-2. Slack 送信の失敗を検出する

Lambda 再試行を無効にしているため、Slack 送信に失敗してもコードは例外を送出せず `MAIL_DUPLICATE_SLACK_FAILED` をログへ出力するだけになる。握り潰しを可視化するためメトリクスフィルタを張る。

ロググループは関数の初回実行時に作られるため、**1回でも実行された後に設定する**。

```bash
aws logs put-metric-filter --log-group-name "/aws/lambda/${FUNCTION_NAME}" --filter-name slack-notification-failed --filter-pattern '"MAIL_DUPLICATE_SLACK_FAILED"' --metric-transformations metricName=SlackNotificationFailed,metricNamespace=MailDuplicateDetector,metricValue=1
```

```bash
aws cloudwatch put-metric-alarm --alarm-name detect-mail-duplicates-slack-failed --namespace MailDuplicateDetector --metric-name SlackNotificationFailed --statistic Sum --period 300 --evaluation-periods 1 --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold --treat-missing-data notBreaching --alarm-actions "${ALARM_TOPIC_ARN}"
```

---

## 7. 動作確認

同一ファイル内に同じ `EmailID` を2行含むログを投入し、2レコードとして保存されることを確認する。ソートキーが衝突すると2件目が上書きされて検知できなくなるため、ここを見る。

```bash
EMAIL_ID="9$(date +%s)"
TS="$(date -u '+%Y-%m-%d %H:%M:%S')"
for _ in 1 2; do printf '[%s] production.INFO: SendEmails [production] success  sending email: %s\n' "$TS" "$EMAIL_ID"; done | gzip > /tmp/verify.log.gz
aws s3 cp /tmp/verify.log.gz "s3://${LOG_BUCKET}/${KEY_PREFIX}verify/$(date +%s).log.gz"
echo "EMAIL_ID=${EMAIL_ID}"
```

数十秒待ってから、レコード数を確認する。**2 でなければソートキーが衝突している。**

```bash
aws dynamodb query --table-name "${TABLE_NAME}" --key-condition-expression 'EmailID = :id' --expression-attribute-values "{\":id\":{\"S\":\"${EMAIL_ID}\"}}" --consistent-read --query 'Items[].recordKey.S'
```

Slack に通知が届いていること、および Lambda のログを確認する。

```bash
aws logs tail "/aws/lambda/${FUNCTION_NAME}" --since 5m
```

確認用に投入した S3 オブジェクトは削除する。DynamoDB のレコードは TTL で1時間後に消える。

---

## 付録A：テーブルを作り直す場合

**DynamoDB のキー構成は作成後に変更できない。** `EmailID` / `recordKey` の構成を変える必要が生じた場合は、テーブルを削除して作り直す。

保持しているのは直近1時間の検知用データのみで、原本ログは S3 にあるため、削除によって失われる情報はない。削除中は検知が止まる。

```bash
aws dynamodb delete-table --table-name "${TABLE_NAME}" && aws dynamodb wait table-not-exists --table-name "${TABLE_NAME}"
```

削除後、手順1 を再実行する。

---

## 付録B：本手順で対応していないこと

| 項目 | 現状 | 備考 |
| --- | --- | --- |
| Slack Webhook URL の保護 | Lambda の環境変数に平文で設定 | マネジメントコンソールと `GetFunctionConfiguration` から参照できる。SSM Parameter Store の SecureString へ移すには、Lambda 側の取得処理の実装が必要 |
| 通知のクールダウン | なし | 複数の Lambda が同時に同じ重複を検知した場合、通知が2回飛ぶことがある（ローカル計測で約20%） |
| S3 イベントの重複配信 | 対応なし | 設計上の対象外 |
| Fluent Bit 再起動によるログ再送 | 対応なし | 設計上の対象外 |
