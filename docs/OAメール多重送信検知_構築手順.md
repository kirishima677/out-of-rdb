# OAメール多重送信検知：構築手順

> STG・本番共通の構築手順。環境の差は「変数」の `ENV_PREFIX` / `ENV_SUFFIX` / `KEY_PREFIX` で吸収する。
> 設計は「OAメール多重送信検知_送信側重複_最小修正版.md」を参照。
> 本番 AWS への操作はブラウザの CloudShell から行う前提で、すべて AWS CLI のコマンドで記述する。
> 手順を再現可能な形で残すことが目的であり、常時稼働の自動化は導入しない。

## 変数

以降のコマンドで使う値。CloudShell のセッションごとに設定する。

```bash
export AWS_REGION=ap-northeast-1
export ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# 環境ごとの接頭辞・接尾辞。
# STGと本番が同一アカウント・同一リージョンにあるため、リソース名を環境で分ける。
# 本番では両方とも空文字にする。
export ENV_PREFIX=stg_      # 本番: export ENV_PREFIX=""
export ENV_SUFFIX=-stg      # 本番: export ENV_SUFFIX=""

export TABLE_NAME="${ENV_PREFIX}mail_send_log_events"
export FUNCTION_NAME="detect-mail-duplicates${ENV_SUFFIX}"
export ROLE_NAME="detect-mail-duplicates-role${ENV_SUFFIX}"
export METRIC_NAMESPACE="MailDuplicateDetector${ENV_SUFFIX}"

# 実環境で確認済みの値
export LOG_BUCKET="hkz-log-archive"
export KEY_PREFIX="service=worker/env=staging/log_source=laravel-app/"   # 本番: env=production

# 以下は貼り付けてから値を埋める。
# `<...>` はシェルのリダイレクトとして解釈されるため、空文字で置いている。
export SLACK_WEBHOOK_URL=""   # Slack Incoming Webhook の URL
export ALARM_TOPIC_ARN=""     # 通知先の SNS トピック ARN
```

STGと本番が同一アカウント・同一リージョンの場合、以下がすべて衝突する。上の変数はこれらを環境ごとに分けるためのものである。

| リソース | 衝突した場合の影響 |
| --- | --- |
| DynamoDB テーブル | STGのテスト送信が本番の判定レコードに混ざる。片方の作り直しが両方に影響する |
| Lambda 関数名 | 後から作った側が既存を上書きする |
| IAM ロール名 | 同上 |
| S3 通知設定の `Id` | 同じバケット内で一意である必要がある。重複すると設定できない |
| CloudWatch アラーム名 | 後から作った側が既存を上書きする |
| メトリクス名前空間 | STGと本番の失敗回数が合算され、区別できなくなる |

`KEY_PREFIX` が `service=worker` である点に注意する。メール送信ジョブ（`EvenSendEmails` / `OddSendEmails`）は Laravel のスケジューラから起動し、**スケジューラが動いているのは worker 側だけ**である。`service=api` を指定すると、Lambda は起動するのにメール送信ログを1件も見つけられず、エラーも出ないまま「重複なし」となる。

`hkz-log-archive` は複数サービス・複数ログソースが同居する共有アーカイブバケットである。実在するプレフィックスは次で確認できる。

```bash
for s in $(aws s3api list-objects-v2 --bucket "${LOG_BUCKET}" --delimiter / --query 'CommonPrefixes[].Prefix' --output text); do
  for e in $(aws s3api list-objects-v2 --bucket "${LOG_BUCKET}" --prefix "$s" --delimiter / --query 'CommonPrefixes[].Prefix' --output text 2>/dev/null); do
    aws s3api list-objects-v2 --bucket "${LOG_BUCKET}" --prefix "$e" --delimiter / --query 'CommonPrefixes[].Prefix' --output text 2>/dev/null | tr '\t' '\n'
  done
done
```

なお実際の S3 キーに**先頭スラッシュは付かない**（Fluent Bit の `s3_key_format` は `/service=...` と記述されているが、出力時に除去される）。確認済みだが、環境が変わった場合は実物で見直すこと。

```bash
aws s3api list-objects-v2 --bucket "${LOG_BUCKET}" --max-keys 1 --query 'Contents[0].Key' --output text
```

### 接続先の確認

変数を設定したら、次の手順へ進む前に必ず実行する。CloudShell はリージョンごとに別セッションであり、別アカウントのコンソールを開いていればそのアカウントへ接続される。

```bash
aws sts get-caller-identity

for v in AWS_REGION ACCOUNT_ID TABLE_NAME FUNCTION_NAME ROLE_NAME METRIC_NAMESPACE LOG_BUCKET KEY_PREFIX SLACK_WEBHOOK_URL ALARM_TOPIC_ARN; do
  printf '  %-18s %s\n' "$v" "$(eval echo \"\${$v:-** 未設定 **}\")"
done
```

`** 未設定 **` が出た変数は、次の区分で判断する。

| 変数 | 扱い |
| --- | --- |
| `AWS_REGION` / `ACCOUNT_ID` / `TABLE_NAME` / `FUNCTION_NAME` / `ROLE_NAME` / `LOG_BUCKET` / `KEY_PREFIX` | **手順1から必須。空のままでは進めない** |
| `METRIC_NAMESPACE` / `ALARM_TOPIC_ARN` | 手順6で必要。SNSトピックが未用意なら手順6を飛ばし、後から実施できる |
| `SLACK_WEBHOOK_URL` | **空のままでよい。** 付録C の手順で構築し、チャンネル用意後に設定する |

特に `ACCOUNT_ID` が空のまま進むと `arn:aws:iam:::role/...` という壊れた ARN が生成され、手順2まで進んでから原因の分かりにくい失敗をする。認証情報が有効かどうかの確認を兼ねているため、ここが空なら先へ進まないこと。

あわせて次の4点を確認する。

| 出力 | 確認内容 |
| --- | --- |
| `Account` | 構築対象のアカウントIDか |
| `Arn` | 想定した IAM プリンシパルか（必要な権限を持つロール／ユーザーか） |
| `REGION` | `ap-northeast-1` か |
| `TABLE` / `FUNCTION` | 接頭辞・接尾辞が意図した環境のものか。STGなら `stg_` と `-stg` が付いているか |

`ENV_PREFIX` / `ENV_SUFFIX` の設定漏れは、**STGの構築が本番のリソースを上書きする**形で影響する。しかも Lambda 関数や IAM ロールは後勝ちで上書きされ、エラーにならない。ここで目視するのは主にそれを防ぐためである。

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

権限は必要最小限に絞る。3つのステートメントで、DynamoDB・S3・CloudWatch Logs のみを許可する。

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

`detect_mail_duplicates.py` をアップロードして関数を作る。ファイルは CloudShell 画面右上の「アクション」→「ファイルのアップロード」で置くか、リポジトリを `git clone` して取得する。カレントディレクトリに配置してから次を実行する。

```bash
zip -j /tmp/detect-mail-duplicates.zip detect_mail_duplicates.py
```

```bash
aws lambda create-function --function-name "${FUNCTION_NAME}" --runtime python3.12 --handler detect_mail_duplicates.handler --role "arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}" --timeout 120 --memory-size 256 --zip-file fileb:///tmp/detect-mail-duplicates.zip --environment "Variables={TABLE_NAME=${TABLE_NAME},TARGET_KEY_PREFIX=${KEY_PREFIX},SLACK_WEBHOOK_URL=${SLACK_WEBHOOK_URL}}"
```

```bash
aws lambda wait function-active-v2 --function-name "${FUNCTION_NAME}"
```

Slack のチャンネルがまだ用意できていない場合は、`SLACK_WEBHOOK_URL` を渡さずに構築を進められる。手順は「付録C：Slack チャンネルが未用意のまま構築する場合」を参照。

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

`--statement-id` は関数内で一意のため、やり直す場合は `ResourceConflictException` になる。先に削除してから再実行する。

```bash
aws lambda remove-permission --function-name "${FUNCTION_NAME}" --statement-id s3-invoke
```

**通知設定を書く前に、既存の設定を必ず退避すること。** `put-bucket-notification-configuration` はバケットの通知設定を**全置換**するため、他の通知が設定されていると消える。

```bash
aws s3api get-bucket-notification-configuration --bucket "${LOG_BUCKET}" | tee /tmp/s3-notification-backup.json
```

出力が空（`{}`）でなければ、その内容に本設定を**追記する形**で編集すること。マージ手順は「付録D-3」を参照。空の場合のみ、以下をそのまま使える。

```bash
cat > /tmp/s3-notification.json <<EOF
{
  "LambdaFunctionConfigurations": [
    {
      "Id": "${FUNCTION_NAME}",
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
aws cloudwatch put-metric-alarm --alarm-name "${FUNCTION_NAME}-errors" --namespace AWS/Lambda --metric-name Errors --dimensions Name=FunctionName,Value="${FUNCTION_NAME}" --statistic Sum --period 300 --evaluation-periods 1 --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold --treat-missing-data notBreaching --alarm-actions "${ALARM_TOPIC_ARN}"
```

### 6-2. Slack 送信の失敗を検出する

Lambda 再試行を無効にしているため、Slack 送信に失敗してもコードは例外を送出せず `MAIL_DUPLICATE_SLACK_FAILED` をログへ出力するだけになる。握り潰しを可視化するためメトリクスフィルタを張る。

ロググループは関数の初回実行時に作られる。この時点ではまだ実行していないため、**先に明示的に作成する**。作成せずにメトリクスフィルタを設定すると `ResourceNotFoundException` になる。Lambda 側にも `logs:CreateLogGroup` を与えてあるので、既に存在していても問題ない。

```bash
aws logs create-log-group --log-group-name "/aws/lambda/${FUNCTION_NAME}" 2>/dev/null || true
```

```bash
aws logs put-metric-filter --log-group-name "/aws/lambda/${FUNCTION_NAME}" --filter-name slack-notification-failed --filter-pattern '"MAIL_DUPLICATE_SLACK_FAILED"' --metric-transformations metricName=SlackNotificationFailed,metricNamespace="${METRIC_NAMESPACE}",metricValue=1
```

```bash
aws cloudwatch put-metric-alarm --alarm-name "${FUNCTION_NAME}-slack-failed" --namespace "${METRIC_NAMESPACE}" --metric-name SlackNotificationFailed --statistic Sum --period 300 --evaluation-periods 1 --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold --treat-missing-data notBreaching --alarm-actions "${ALARM_TOPIC_ARN}"
```

---

## 7. 動作確認

同一ファイル内に同じ `EmailID` を2行含むログを投入し、2レコードとして保存されることを確認する。ソートキーが衝突すると2件目が上書きされて検知できなくなるため、ここを見る。

STG では送信対象のメールが常に0件のため、実際の `success sending email:` ログは出力されない。検証は本番と同形式の合成ログで行う。

```bash
export EMAIL_ID="9$(date +%s)"
export ENV_NAME=staging        # 本番: production

python3 - <<'PY' | gzip > /tmp/verify.log.gz
import json, os
from datetime import datetime, timezone

now = datetime.now(timezone.utc)
email_id, env = os.environ["EMAIL_ID"], os.environ["ENV_NAME"]
ctx = {"mail_object_type": "Notification", "mail_object_id": 0, "subject": "検証用",
       "scheduled_at": now.strftime("%Y-%m-%d %H:%M:%S"),
       "content": f"Email {email_id} was successfully sent!"}
msg = (f'[{now.strftime("%Y-%m-%d %H:%M:%S")}] {env}.INFO: '
       f'EvenSendEmails [{env}] success  sending email: {email_id} '
       f'{json.dumps(ctx, ensure_ascii=False)}  \n')
for host in ("i-verify0000000001", "i-verify0000000002"):
    print(json.dumps({"date": now.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
                      "log_source": "laravel.app", "log": msg, "service": "worker",
                      "env": env, "instance_id": host}, ensure_ascii=False))
PY
```

**投入先はパーティション構造（`year=/month=/day=/hour=`）に沿わせること。** このバケットには Glue / Athena による集計が乗っている可能性があり、構造から外れたパスはパーティション認識を壊しうる。検証用であることはファイル名の `verify-` で区別する。

```bash
Y=$(date -u +%Y); M=$(date -u +%m); D=$(date -u +%d); H=$(date -u +%H)
export VERIFY_DIR="${KEY_PREFIX}year=${Y}/month=${M}/day=${D}/hour=${H}"
aws s3 cp /tmp/verify.log.gz "s3://${LOG_BUCKET}/${VERIFY_DIR}/verify-$(date +%s).gz"
echo "EMAIL_ID=${EMAIL_ID}"
```

数十秒待ってから、レコード数を確認する。

```bash
aws dynamodb query --table-name "${TABLE_NAME}" --key-condition-expression 'EmailID = :id' --expression-attribute-values "{\":id\":{\"S\":\"${EMAIL_ID}\"}}" --consistent-read --query 'Items[].recordKey.S'
```

件数ごとに原因が異なる。

| 件数 | 判定 |
| --- | --- |
| 2 | 正常 |
| 1 | **ソートキーが衝突している。** `recordKey` に `sourceKey` と `sourceLineNumber` が含まれているか確認する |
| 0 | **Lambda が起動していない。** `KEY_PREFIX` が実際の S3 キーと一致していない、S3 通知設定が反映されていない、IAM 権限が不足している、のいずれか |

実運用で重複を検知した際は、`sourceHost` を見ることで「別インスタンスでの二重起動」か「同一インスタンスでの二重送信」かを切り分けられる。

```bash
aws dynamodb query --table-name "${TABLE_NAME}" --key-condition-expression 'EmailID = :id' --expression-attribute-values "{\":id\":{\"S\":\"<EmailID>\"}}" --consistent-read --query 'Items[].{host:sourceHost.S,at:createdAt.S,src:sourceKey.S}'
```

0件の場合は、まず Lambda が呼ばれたかどうかを次のログで切り分ける。

Slack に通知が届いていること、および Lambda のログを確認する。

```bash
aws logs tail "/aws/lambda/${FUNCTION_NAME}" --since 5m
```

確認用に投入した S3 オブジェクトは削除する。ログアーカイブに偽のログを残すと、後の調査や集計を汚す。まず対象を一覧で確認する。

```bash
aws s3 ls "s3://${LOG_BUCKET}/${VERIFY_DIR}/" | awk '/verify-/{print $4}'
```

内容を確認してから削除する。

```bash
aws s3 ls "s3://${LOG_BUCKET}/${VERIFY_DIR}/" | awk '/verify-/{print $4}' | while read -r f; do
  aws s3 rm "s3://${LOG_BUCKET}/${VERIFY_DIR}/${f}"
done
```

DynamoDB のレコードは TTL で1時間後に消えるため、削除は不要。

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

---

## 付録C：Slack チャンネルが未用意のまま構築する場合

Webhook URL がまだ無くても構築を進められる。検知処理は完全に動作し、通知内容は CloudWatch Logs へ出力される。Lambda もエラーにならない。

### C-1. `SLACK_WEBHOOK_URL` を渡さずに関数を作る

手順3 の `create-function` から `SLACK_WEBHOOK_URL` を外す。

```bash
aws lambda create-function --function-name "${FUNCTION_NAME}" --runtime python3.12 --handler detect_mail_duplicates.handler --role "arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}" --timeout 120 --memory-size 256 --zip-file fileb:///tmp/detect-mail-duplicates.zip --environment "Variables={TABLE_NAME=${TABLE_NAME},TARGET_KEY_PREFIX=${KEY_PREFIX}}"
```

この状態で重複を検知すると、通知本文を Lambda ログへ出力したうえで次の1行を残し、正常終了する。

```text
SLACK_WEBHOOK_URL is not set; skip Slack notification.
```

検知できているかはログで確認する。

```bash
aws logs tail "/aws/lambda/${FUNCTION_NAME}" --since 10m --filter-pattern "Duplicate detected"
```

### C-2. 送信失敗の経路を検証する

**未設定は「送信失敗」ではない。** コードは送信を試みずに抜けるため `MAIL_DUPLICATE_SLACK_FAILED` は出力されず、手順6-2 のメトリクスフィルタもアラームも発火しない。

| `SLACK_WEBHOOK_URL` | ログ出力 | アラーム |
| --- | --- | --- |
| 未設定・空文字 | `SLACK_WEBHOOK_URL is not set; skip` | 発火しない |
| 到達できない URL | `MAIL_DUPLICATE_SLACK_FAILED ...` | **発火する** |

チャンネルが未用意の期間は、アラーム経路を検証する好機でもある。到達できない URL を一時的に設定して重複を発生させれば、`MAIL_DUPLICATE_SLACK_FAILED` → メトリクスフィルタ → アラームまでを通しで確認できる。

`.invalid` は RFC 2606 で予約されたトップレベルドメインであり、名前解決に必ず失敗する。外部サービスへ依存せず再現できる。

```bash
aws lambda update-function-configuration --function-name "${FUNCTION_NAME}" --environment "Variables={TABLE_NAME=${TABLE_NAME},TARGET_KEY_PREFIX=${KEY_PREFIX},SLACK_WEBHOOK_URL=https://slack-webhook-not-configured.invalid/test}"
```

手順7 の動作確認を実行してから、ログとアラームを確認する。

```bash
aws logs tail "/aws/lambda/${FUNCTION_NAME}" --since 10m --filter-pattern "MAIL_DUPLICATE_SLACK_FAILED"
```

```bash
aws cloudwatch describe-alarms --alarm-names "${FUNCTION_NAME}-slack-failed" --query 'MetricAlarms[].{State:StateValue,Reason:StateReason}'
```

注意点が2つある。

- **メトリクスフィルタは設定後に出力されたログにのみ適用される。** 手順6-2 を先に済ませてから重複を発生させること
- アラームが `OK` から `ALARM` へ変わるまで、評価期間ぶん（5分）の待ち時間がある

アラームの確認には手順6が完了している必要がある。SNS トピックが未用意で手順6を飛ばしている場合は、ログ出力の確認までとなる。

### C-3. チャンネル用意後に設定する

**`update-function-configuration --environment` は環境変数マップを全置換する。** `SLACK_WEBHOOK_URL` を追加する際に他の変数を書き忘れると、その変数は消える。

特に `TABLE_NAME` が消えると既定値の `mail_send_log_events` へフォールバックし、**STG の Lambda が本番のテーブルへ書き込む**。エラーは出ないため気づけない。

必ず全変数を列挙すること。

```bash
aws lambda update-function-configuration --function-name "${FUNCTION_NAME}" --environment "Variables={TABLE_NAME=${TABLE_NAME},TARGET_KEY_PREFIX=${KEY_PREFIX},SLACK_WEBHOOK_URL=${SLACK_WEBHOOK_URL}}"
```

更新後、反映内容を必ず確認する。

```bash
aws lambda wait function-updated-v2 --function-name "${FUNCTION_NAME}" && aws lambda get-function --function-name "${FUNCTION_NAME}" --query 'Configuration.Environment.Variables'
```

---

## 付録D：構築後に設定を変更する場合

### D-1. 変更操作ごとの影響範囲

`update-function-configuration` は**渡したパラメータだけ**を更新する。タイムアウトだけを変えても環境変数は保たれる。ただし `--environment` を渡した場合、環境変数マップは**渡した内容で丸ごと置き換わる**。

| 操作 | 影響範囲 | 注意 |
| --- | --- | --- |
| `update-function-code` | コードのみ | 環境変数・タイムアウトは保たれる |
| `update-function-configuration --timeout` / `--memory-size` | 指定した項目のみ | 環境変数は保たれる |
| `update-function-configuration --environment` | **環境変数マップ全体** | 書かなかった変数は**消える** |
| `put-function-event-invoke-config` | 再試行設定のみ | こちらも渡した項目のみ |
| `put-bucket-notification-configuration` | **バケットの通知設定全体** | 他の環境・他ログソースの通知も**消える** |

下2つが事故になりやすい。どちらもエラーを出さずに設定が失われる。

### D-2. 環境変数を変更する

`TABLE_NAME` が消えると既定値の `mail_send_log_events` へフォールバックし、**STG の Lambda が本番のテーブルへ書き込む**。全変数を手で書き直すのではなく、現在値を読んでマージするほうが安全である。

```bash
NEW_ENV="$(aws lambda get-function-configuration --function-name "${FUNCTION_NAME}" --query 'Environment.Variables' --output json \
  | jq -c --arg k "SLACK_WEBHOOK_URL" --arg v "${SLACK_WEBHOOK_URL}" '.[$k] = $v')"
echo "${NEW_ENV}"
```

出力を目視し、**変更したい変数以外がすべて残っていること**を確認してから適用する。

```bash
aws lambda update-function-configuration --function-name "${FUNCTION_NAME}" --environment "{\"Variables\":${NEW_ENV}}"
```

```bash
aws lambda wait function-updated-v2 --function-name "${FUNCTION_NAME}" && aws lambda get-function --function-name "${FUNCTION_NAME}" --query 'Configuration.Environment.Variables'
```

`jq` は CloudShell に標準で入っている。削除したい場合は `'del(.KEY)'`、複数同時に変えたい場合は `'.A=$a | .B=$b'` のように書く。

### D-3. S3 通知設定を変更する

**これがもっとも影響が大きい。** `put-bucket-notification-configuration` はバケットの通知設定を全置換するため、STG の設定を追加するつもりで**本番の通知を消す**ことが起こりうる。消えても S3 も Lambda もエラーを出さず、本番の検知だけが無言で止まる。

必ず現在値を退避してからマージする。

```bash
aws s3api get-bucket-notification-configuration --bucket "${LOG_BUCKET}" | jq 'del(.ResponseMetadata)' > /tmp/s3-notif-before.json
cat /tmp/s3-notif-before.json
```

追加または更新したい1件を作る。

```bash
cat > /tmp/s3-notif-entry.json <<EOF
{
  "Id": "${FUNCTION_NAME}",
  "LambdaFunctionArn": "arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}",
  "Events": ["s3:ObjectCreated:*"],
  "Filter": { "Key": { "FilterRules": [ { "Name": "prefix", "Value": "${KEY_PREFIX}" } ] } }
}
EOF
```

同じ `Id` の既存エントリを差し替え、それ以外は残す形でマージする。

```bash
jq --slurpfile e /tmp/s3-notif-entry.json '
  .LambdaFunctionConfigurations =
    (((.LambdaFunctionConfigurations // []) | map(select(.Id != $e[0].Id))) + [$e[0]])
' /tmp/s3-notif-before.json > /tmp/s3-notif-after.json
cat /tmp/s3-notif-after.json
```

**他の環境のエントリが残っていることを目視してから**適用する。

```bash
aws s3api put-bucket-notification-configuration --bucket "${LOG_BUCKET}" --notification-configuration file:///tmp/s3-notif-after.json
```

適用後に実際の設定を読み直して確認する。

```bash
aws s3api get-bucket-notification-configuration --bucket "${LOG_BUCKET}" --query 'LambdaFunctionConfigurations[].{Id:Id,Prefix:Filter.Key.FilterRules[0].Value}'
```

問題があれば退避したファイルで戻せる。

```bash
aws s3api put-bucket-notification-configuration --bucket "${LOG_BUCKET}" --notification-configuration file:///tmp/s3-notif-before.json
```

`/tmp` は CloudShell のセッション終了で消える。作業を中断する可能性があるなら、退避先を `~/` にすること。

### D-4. 変更できないもの

| 項目 | 対応 |
| --- | --- |
| DynamoDB のキー構成（`EmailID` / `recordKey`） | 変更不可。付録A の手順でテーブルを作り直す |
| DynamoDB のテーブル名 | 変更不可。新しい名前で作り直し、Lambda の `TABLE_NAME` を D-2 の手順で更新する |
| Lambda の関数名 | 変更不可。作り直しとなる。ロググループ・アラーム名も関数名に紐づくため合わせて作り直す |

### D-5. 変更後の確認

環境変数・プレフィックス・通知設定のいずれを変えた場合も、**手順7 の動作確認を再実行する**。設定の反映を API で確認できても、経路全体が通っているかは実際にログを流さないと分からない。

設定変更の直後は、Lambda の実行環境が入れ替わるまでにわずかな時間差がある。モジュール読み込み時に読まれる `TABLE_NAME` や `TARGET_KEY_PREFIX` は、切り替わるまで旧値が使われることがある。**変更直後に動作確認して想定と違う場合は、数十秒おいて再実行する。**
