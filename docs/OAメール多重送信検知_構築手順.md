# OAメール多重送信検知：構築手順

> STG・本番共通の構築手順。環境の差は「変数」の `ENV_PREFIX` / `ENV_SUFFIX` / `KEY_PREFIX` で吸収する。
> 設計は「OAメール多重送信検知_送信側重複_最小修正版.md」を参照。
> 本番 AWS への操作はブラウザの CloudShell から行う前提で、すべて AWS CLI のコマンドで記述する。
> 手順を再現可能な形で残すことが目的であり、常時稼働の自動化は導入しない。
> 構築後の確認・調査で使うコマンドは「OAメール多重送信検知_運用コマンド集.md」にまとめている。

## 変数

以降のコマンドで使う値。CloudShell のセッションごとに設定する。

```bash
export AWS_REGION=ap-northeast-1
export ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# 環境ごとの接頭辞・接尾辞。
# STGと本番が同一アカウント・同一リージョンにあるため、リソース名を環境で分ける。
# どちらの環境でも必ず値を入れる。空文字は使わない（理由は下記）。
export ENV_PREFIX=stg_      # 本番: export ENV_PREFIX=prd_
export ENV_SUFFIX=-stg      # 本番: export ENV_SUFFIX=-prd

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
export ALARM_TOPIC_ARN=""     # 通知先の SNS トピック ARN。STG では空のままでよい（手順6-0）
```

**本番を空文字にしない。** Lambda のコードは `TABLE_NAME` が未設定のとき既定値 `mail_send_log_events` へフォールバックする。本番を空文字で命名すると、この既定値が**実在する本番テーブルを指す**。環境変数の設定漏れや `--environment` の書き落としが、エラーを出さないまま本番データへの書き込みになる。

`prd_` を付けておけば、既定値 `mail_send_log_events` は AWS 上のどの環境にも存在しない名前になる。設定が欠けた時点で `ResourceNotFoundException` となり、手順6-1 の `Errors` アラームで検出できる。**沈黙して誤書き込みするより、落ちるほうがよい。**

なお既定値の `mail_send_log_events` は、ローカルサンドボックス（DynamoDB Local）が実際に使う名前である。AWS 側で使わないことに意味がある。

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

### 2-1. CloudWatch Logs への書き込み権限

**AWS 管理ポリシーを使う。** ログ関連の権限をインラインポリシーで自前定義すると、`logs:CreateLogGroup` のリソース指定が合わずロググループが作られないことがある。**その状態では Lambda は実行されるのにログが一切残らず、原因調査が極めて困難になる。**

```bash
aws iam attach-role-policy --role-name "${ROLE_NAME}" --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam list-attached-role-policies --role-name "${ROLE_NAME}" --query 'AttachedPolicies[].PolicyName' --output text
```

`AWSLambdaBasicExecutionRole` が返れば正常。

### 2-2. DynamoDB と S3 の権限

インラインポリシーは2つのステートメントに絞る。ログ権限は 2-1 で付与済みのため含めない。

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
    }
  ]
}
EOF
cat /tmp/lambda-policy.json
```

変数がすべて展開されていることを目視してから適用する。

```bash
aws iam put-role-policy --role-name "${ROLE_NAME}" --policy-name detect-mail-duplicates --policy-document file:///tmp/lambda-policy.json
```

**適用できたことを必ず確認する。** このコマンドは成功しても何も出力しないため、実行し忘れても気づけない。ポリシーが無いまま進むと、後続の手順はすべて成功したように見えたうえで、**Lambda が S3 を読めず AccessDenied で失敗する**。

```bash
aws iam get-role-policy --role-name "${ROLE_NAME}" --policy-name detect-mail-duplicates --query 'PolicyDocument.Statement[].{Sid:Sid,Resource:Resource}' --output table
```

2行（`DetectionTableAccess` / `ReadMailSendLogs`）が返れば正常。`NoSuchEntity` が返る場合は適用されていない。

S3 の権限をプレフィックス限定にしているのは、この Lambda が他のログソースを読めないようにするため。

---

## 3. Lambda 関数

`detect_mail_duplicates.py` をアップロードして関数を作る。ファイルは CloudShell 画面右上の「アクション」→「ファイルのアップロード」で置くか、リポジトリを `git clone` して取得する。カレントディレクトリに配置してから次を実行する。

```bash
zip -j /tmp/detect-mail-duplicates.zip detect_mail_duplicates.py
```

環境変数はショートハンド記法（`Variables={K=V,...}`）ではなく JSON で渡す。**ショートハンド記法は空の値を解釈できず、`SLACK_WEBHOOK_URL` が未設定だと `ParamValidation` エラーになる**ためである。次の構築方法なら、空の変数は自動的に除外される。

```bash
export TARGET_KEY_PREFIX="${KEY_PREFIX}"
ENV_JSON="$(python3 -c 'import json,os; print(json.dumps({"Variables":{k:os.environ[k] for k in ("TABLE_NAME","TARGET_KEY_PREFIX","SLACK_WEBHOOK_URL") if os.environ.get(k)}}))')"
echo "${ENV_JSON}"
```

出力を目視し、設定したい変数が入っていることを確認してから実行する。

```bash
aws lambda create-function --function-name "${FUNCTION_NAME}" --runtime python3.12 --handler detect_mail_duplicates.handler --role "arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}" --timeout 300 --memory-size 1024 --zip-file fileb:///tmp/detect-mail-duplicates.zip --environment "${ENV_JSON}"
```

```bash
aws lambda wait function-active-v2 --function-name "${FUNCTION_NAME}"
```

Slack のチャンネルがまだ用意できていない場合は、`SLACK_WEBHOOK_URL` を空のままにしておけばよい。上の方法で自動的に除外される。検証の進め方は「付録C：Slack チャンネルが未用意のまま構築する場合」を参照。

**メモリ 1024MB・タイムアウト 300 秒の根拠**。いずれも STG での実測値に基づく。

処理時間はマッチした行数に比例する。1行につき DynamoDB への書き込み1回と強整合読み取り1回、計2回のネットワーク往復が発生するためである。マッチしない行は正規表現を通すだけなので、コストを決めるのは**送信通数そのもの**になる。

600行の合成ログでの実測は次のとおり。

| メモリ | Duration | 1行あたり |
| --- | --- | --- |
| 256 MB | 20,110 ms | 33.5 ms |
| 1024 MB | 6,320 ms | 10.5 ms |

メモリ使用量はいずれも 102MB で頭打ちしており、差は CPU とネットワーク帯域の配分による。**ローカル（LocalStack）での 4.2ms は同一ホスト内通信の値であり、実環境の見積もりには使えない。** 8倍の開きがある。

1024MB を採ると、想定される流量は次のようになる。

| 状況 | 送信ログ行数 | 推定 Duration |
| --- | --- | --- |
| 通常（10分フラッシュ） | 800 | 8秒 |
| ピーク（3台フル稼働 240通/分 × 10分） | 2,400 | 25秒 |
| 30分滞留後の一括フラッシュ | 7,200 | 76秒 |
| ピーク流量で2時間滞留 | 28,800 | 302秒 |

課金は GB-秒であるため 1024MB は 256MB の約1.37倍のコストになるが、メール送信ログのオブジェクトは1時間あたり数件であり、月額では1ドル未満の差にとどまる。

タイムアウトを 300 秒としているのは最後の行に備えるためである。**課金は実際の実行時間に対して発生し、設定値には課金されない。** 再試行を無効にしているため、タイムアウトすると**そのオブジェクトは丸ごと検知対象から落ちる**（手順6-1 の `Errors` アラームでは検出できるが、データは戻らない）。上げる副作用がほぼ無いのに対し、失われるものが大きい。

行数そのものを抑えたい場合は、Fluent Bit のフラッシュ間隔を短くするのが最も確実である。1分間隔にすればピークでも240行/オブジェクトに収まる。

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

### 5-1. S3 が Lambda を呼べるようにする

```bash
aws lambda add-permission --function-name "${FUNCTION_NAME}" --statement-id s3-invoke --action lambda:InvokeFunction --principal s3.amazonaws.com --source-arn "arn:aws:s3:::${LOG_BUCKET}" --source-account "${ACCOUNT_ID}"
```

> **やり直す場合のみ。** `--statement-id` は関数内で一意のため、再実行すると `ResourceConflictException` になる。その場合は先に削除してから再実行する。通常の構築では実行不要。
>
> ```bash
> aws lambda remove-permission --function-name "${FUNCTION_NAME}" --statement-id s3-invoke
> ```

### 5-2. 1バケットに登録できるのは1つだけ

**このバケットに通知設定を登録できる消費者は、実質1つに限られる。** 本手順で最も制約が強い箇所であり、環境構成の前提になる。

S3 には次の制約がある。

> Configurations on the same bucket cannot share a common event type.

同一バケット・同一イベント種別で**条件が重なる通知設定は共存できない**。フィルタなしの設定同士は完全に重なるため、2つ目を登録しようとすると `InvalidArgument` で拒否される。

環境ごとに分けるにはプレフィックスフィルタが必要になるが、**実測ではフィルタを付けると本番・STG のどちらにもイベントが届かなくなった**。設定適用から15分以上待っても変わらない。イベント中のオブジェクトキーが URL エンコードされている（`service%3Dworker/...`）ため、フィルタ側も同じ形で評価されている可能性があるが、**未検証である**。

```json
{ "Name": "Prefix", "Value": "service=worker/env=production/log_source=laravel-app/" }
```

この値では1件も配信されなかった。

結果として採用した構成は次のとおり。

| 項目 | 内容 |
| --- | --- |
| 登録するエントリ | 本番のみ1件 |
| フィルタ | **付けない** |
| 届くイベント | バケット内の全オブジェクト作成 |
| 対象の絞り込み | Lambda 側の `TARGET_KEY_PREFIX` |

**データの分離は二重に担保されている。** 対象外のイベントを受け取っても中身は読めない。

1. コードが `TARGET_KEY_PREFIX` で判定し、`GetObject` を呼ぶ前に捨てる
2. IAM の `s3:GetObject` が本番プレフィックス配下に限定されている。仮にコードの判定が壊れても `AccessDenied` で止まる

代償は2つある。

- **無関係なオブジェクトでも Lambda が起動する。** 毎時150回程度。金額は月20〜30円で問題にならないが、`Received S3 event` がイベント全文とともに出力されるため**ログのノイズが大きい**
- **STG を同時に稼働させられない。** STG の通知エントリは削除した。再開するには本番のエントリと付け替えるか、フィルタの問題を解決する必要がある

### 5-3. 通知設定をマージして適用する

`put-bucket-notification-configuration` はバケットの通知設定を**全置換**する。`hkz-log-archive` は複数サービス・複数ログソースが同居する共有アーカイブバケットであり、他チームの通知が設定されている可能性がある。**消えても S3 も Lambda もエラーを出さず、その通知だけが無言で止まる。**

そのため、既存設定を必ず退避し、マージしてから適用する。既存が空（`{}`）でも同じ手順で動くため、場合分けは不要である。

現在の設定を退避する。CloudShell の `/tmp` はセッション終了で消えるため、`~/` に置く。

```bash
aws s3api get-bucket-notification-configuration --bucket "${LOG_BUCKET}" | jq 'del(.ResponseMetadata)' > ~/s3-notif-before.json
[ -s ~/s3-notif-before.json ] || echo '{}' > ~/s3-notif-before.json
cat ~/s3-notif-before.json
```

**2行目は必須である。** 通知設定が1件も無いバケットでは、AWS CLI v2 は `{}` ではなく**何も出力しない**。そのまま進めると退避ファイルが0バイトになり、次のマージ結果も空になる。適用しても通知設定が入らず、しかもエラーは出ない。

退避ファイルが `{}` と表示されれば、そのバケットに通知設定は1件も無い。他の設定を壊す心配がないため、そのまま進めてよい。

なお無出力が「設定なし」なのか「コマンドの失敗」なのかは終了コードで判別できる。

```bash
aws s3api get-bucket-notification-configuration --bucket "${LOG_BUCKET}"; echo "exit=$?"
```

追加するエントリを作る。

```bash
cat > ~/s3-notif-entry.json <<ENTRY
{
  "Id": "${FUNCTION_NAME}",
  "LambdaFunctionArn": "arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}",
  "Events": ["s3:ObjectCreated:*"]
}
ENTRY
cat ~/s3-notif-entry.json
```

**`Filter` は付けない。** 理由は 5-2 のとおり。付けると配信されなくなる。

同じ `Id` の既存エントリを差し替え、それ以外は残す形でマージする。

```bash
jq --slurpfile e ~/s3-notif-entry.json '
  .LambdaFunctionConfigurations =
    (((.LambdaFunctionConfigurations // []) | map(select(.Id != $e[0].Id))) + [$e[0]])
' ~/s3-notif-before.json > ~/s3-notif-after.json

jq -r '.LambdaFunctionConfigurations[] | "\(.Id)  →  \(.Filter.Key.FilterRules[0].Value // "(フィルタなし)")"' ~/s3-notif-after.json
```

**既存のエントリが残っていることを目視してから**適用する。

別環境のエントリが残っている場合、フィルタなし同士で重なるため適用は拒否される。その場合は、消す対象を明示して取り除く。**`LambdaFunctionConfigurations` を丸ごと置き換える書き方は避ける。** 他チームのエントリを巻き込む。

```bash
jq --arg id "${FUNCTION_NAME}" --arg dropid "detect-mail-duplicates-stg" '
  .LambdaFunctionConfigurations =
    ((.LambdaFunctionConfigurations // [])
      | map(select(.Id != $dropid))
      | map(if .Id == $id then del(.Filter) else . end))
' ~/s3-notif-before.json > ~/s3-notif-after.json
```

`file://` の後ろではチルダが展開されないため、`${HOME}` を使う。

```bash
aws s3api put-bucket-notification-configuration --bucket "${LOG_BUCKET}" --notification-configuration "file://${HOME}/s3-notif-after.json"
```

適用後、実際の設定を読み直して確認する。

```bash
aws s3api get-bucket-notification-configuration --bucket "${LOG_BUCKET}" --query 'LambdaFunctionConfigurations[].{Id:Id,Prefix:Filter.Key.FilterRules[0].Value}'
```

### 5-4. 反映を待つ

**通知設定の変更が実際にイベント配信へ反映されるまで、数分かかる。** API 上は即座に設定が読み出せるため反映済みに見えるが、この間にオブジェクトを置いてもイベントは発火しない。本番構築時の実測では、適用から配信開始まで約7分だった。

**S3 イベントはオブジェクト作成時にしか発火しない。** 反映前に置いたオブジェクトは遡って処理されないため、確認は必ず新しいオブジェクトで行う。

```bash
echo "5分待機"; sleep 300
```

**この待機を省くと、手順7 の動作確認が理由不明で失敗する。** 設定・権限・プレフィックスがすべて正しくても0件になるため、原因の切り分けに時間を取られる。通知設定を変更したら必ず待つこと。

待機中に、実ログでイベントが届き始めているかを確認できる。

```bash
aws logs tail "/aws/lambda/${FUNCTION_NAME}" --since 5m --follow
```

問題があれば退避したファイルで戻せる。

```bash
aws s3api put-bucket-notification-configuration --bucket "${LOG_BUCKET}" --notification-configuration "file://${HOME}/s3-notif-before.json"
```

プレフィックスフィルタを設定するのは、同じバケットに入る他のログソース（nginx、php-fpm など）で Lambda を起動させないため。Lambda 側にも同じプレフィックスの判定があるが、そちらは設定漏れに備えた保険であり、起動自体は防げない。

---

## 6. CloudWatch アラーム

### 6-0. 通知先（SNS）の要否

この章は3つの部品からなる。**環境によって必要なものが異なる。**

| 部品 | STG | 本番 | 理由 |
| --- | --- | --- | --- |
| メトリクスフィルタ | **必要** | 必要 | フィルタパターンが実ログに当たるかは、ここでしか確かめられない |
| アラーム定義 | 推奨 | 必要 | しきい値と評価の確認。1つあたり月額0.1ドル程度 |
| SNS トピック + 購読 | 不要 | 必要 | 通知先がSTGに存在しない |

**メトリクスフィルタだけは STG で省略しない。** `MAIL_DUPLICATE_SLACK_FAILED` はこのためだけにコードへ埋め込んだ固定文字列であり、フィルタが当たらなければ Slack 送信の失敗は完全に無言のまま消える。フィルタパターンの構文は取り違えやすく（付録E-1 に、同系統の `filter-log-events --filter-pattern` で誤判断した記録がある）、**本番で初めて実行して当たらなかったことに気づけない**のが最悪の形になる。

一方でアラームと SNS は、メトリクスにしきい値を乗せる定型部分であり、失敗する要素がほとんどない。

`put-metric-alarm` の `--alarm-actions` は省略できる。通知先が無くてもアラームは評価され状態遷移するため、**「メトリクスが立つ → アラームが ALARM になる」までを SNS 抜きで通しで確認できる。**

以降のコマンドを両環境で共通にするため、通知先の有無を変数に寄せる。`ALARM_TOPIC_ARN` が空なら `--alarm-actions` ごと付かない。

```bash
ALARM_ACTIONS=""
[ -n "${ALARM_TOPIC_ARN}" ] && ALARM_ACTIONS="--alarm-actions ${ALARM_TOPIC_ARN}"
echo "ALARM_ACTIONS=[${ALARM_ACTIONS}]"
```

STG で `ALARM_ACTIONS=[]` と出れば想定どおり。本番で空になっている場合は、変数ブロックの `ALARM_TOPIC_ARN` が未設定である。

> `${ALARM_ACTIONS}` は意図的に引用符を付けずに展開する。引用すると空文字が1つの引数として渡り、`ParamValidation` エラーになる。ARN に空白は含まれないため、この使い方で問題ない。

アラーム名を変えずに `put-metric-alarm` を再実行すれば上書き更新になる。後から通知先を足す場合も、同じコマンドをもう一度流せばよい。

### 6-1. Lambda の失敗を検出する

検知パイプライン自体が停止しても、そのままでは「重複がない」状態と区別がつかない。タイムアウトで落ちた場合も、再試行を無効にしているため**そのオブジェクトは丸ごと検知対象から落ちる**。ここで拾う。

```bash
aws cloudwatch put-metric-alarm --alarm-name "${FUNCTION_NAME}-errors" --namespace AWS/Lambda --metric-name Errors --dimensions Name=FunctionName,Value="${FUNCTION_NAME}" --statistic Sum --period 300 --evaluation-periods 1 --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold --treat-missing-data notBreaching ${ALARM_ACTIONS}
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
aws cloudwatch put-metric-alarm --alarm-name "${FUNCTION_NAME}-slack-failed" --namespace "${METRIC_NAMESPACE}" --metric-name SlackNotificationFailed --statistic Sum --period 300 --evaluation-periods 1 --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold --treat-missing-data notBreaching ${ALARM_ACTIONS}
```

**メトリクスフィルタは設定後に出力されたログにしか適用されない。** ここを済ませてから、付録F-6 の失敗テストへ進むこと。順序を逆にすると、フィルタが正しくても何も立たない。

### 6-3. 設定内容を確認する

`put-metric-filter` と `put-metric-alarm` はいずれも成功時に何も出力しない。実行し忘れに気づけないため、必ず確認する。

```bash
aws logs describe-metric-filters --log-group-name "/aws/lambda/${FUNCTION_NAME}" --query 'metricFilters[].{Name:filterName,Pattern:filterPattern}' --output table
```

```bash
aws cloudwatch describe-alarms --alarm-names "${FUNCTION_NAME}-errors" "${FUNCTION_NAME}-slack-failed" --query 'MetricAlarms[].{Name:AlarmName,State:StateValue,Actions:AlarmActions}' --output table
```

作成直後は `INSUFFICIENT_DATA` である。`Actions` は、STG では空、本番では SNS トピックの ARN が入る。

フィルタが実際に機能するかは、この時点ではまだ確認できていない。**実際に失敗を起こして初めて確定する**（付録F-6）。

---

## 7. 動作確認

同一ファイル内に同じ `EmailID` を2行含むログを投入し、2レコードとして保存されることを確認する。ソートキーが衝突すると2件目が上書きされて検知できなくなるため、ここを見る。

**STG でも実際の送信ログは出力される。** 2026-09-17 の実測では `service=worker/env=staging/log_source=laravel-app/` に 9/16 ぶんで11件（Even 5 / Odd 6）あった。

ただし件数が少なく発生タイミングも任意ではないため、**検知ロジックの動作確認には合成ログを使う**。アプリ側のログ出力形式を検証する場合は実ログを待つ。

```bash
Y=$(date -u +%Y); M=$(date -u +%m); D=$(date -u +%d)
BASE="service=worker/env=staging/log_source=laravel-app/year=${Y}/month=${M}/day=${D}/"
rm -rf /tmp/stglog; mkdir -p /tmp/stglog
aws s3 cp "s3://${LOG_BUCKET}/${BASE}" /tmp/stglog/ --recursive --quiet
find /tmp/stglog -name '*.gz' -print0 | xargs -0 zcat 2>/dev/null | grep -cE 'success +sending email:'
```

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

ここまでで検知そのものは確認できる。Slack 通知とアラーム経路の検証は「付録F」、本番ログを使ったリプレイと負荷確認は「付録G」を参照。

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
| 通知のクールダウン | なし | 複数の Lambda が同時に同じ重複を検知した場合、通知が2回飛ぶことがある（ローカル計測で約20%）。後続オブジェクトごとに再通知される点は付録F-8 を参照 |
| S3 イベントの重複配信 | 対応なし | 設計上の対象外 |
| Fluent Bit によるログ再送 | 対応なし | **2026-09-15 に実際に発生した。**9/10 に出力されたログ行 4,666行が5日遅れで再送され、S3 に同じ行が2つのパーティションへ格納された状態になっている。今回は間隔が5日あり判定窓の外だったため誤検知は起きていないが、**1時間以内に再送されると「送っていないメールを重複」と通知する**。検知稼働後（9/16以降）の再送は観測されていない |

---

## 付録C：Slack チャンネルが未用意のまま構築する場合

Webhook URL がまだ無くても構築を進められる。検知処理は完全に動作し、通知内容は CloudWatch Logs へ出力される。Lambda もエラーにならない。

### C-1. `SLACK_WEBHOOK_URL` を渡さずに関数を作る

`SLACK_WEBHOOK_URL` を空のままにして、手順3 をそのまま実行する。環境変数の JSON を組み立てる際に、空の変数は自動的に除外される。

```bash
export TARGET_KEY_PREFIX="${KEY_PREFIX}"
ENV_JSON="$(python3 -c 'import json,os; print(json.dumps({"Variables":{k:os.environ[k] for k in ("TABLE_NAME","TARGET_KEY_PREFIX","SLACK_WEBHOOK_URL") if os.environ.get(k)}}))')"
echo "${ENV_JSON}"
```

```text
{"Variables": {"TABLE_NAME": "stg_mail_send_log_events", "TARGET_KEY_PREFIX": "service=worker/env=staging/log_source=laravel-app/"}}
```

`SLACK_WEBHOOK_URL` が含まれていないことを確認してから、手順3 の `create-function` を実行する。

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
SLACK_WEBHOOK_URL="https://slack-webhook-not-configured.invalid/test" TARGET_KEY_PREFIX="${KEY_PREFIX}" \
  ENV_JSON="$(python3 -c 'import json,os; print(json.dumps({"Variables":{k:os.environ[k] for k in ("TABLE_NAME","TARGET_KEY_PREFIX","SLACK_WEBHOOK_URL") if os.environ.get(k)}}))')" \
  sh -c 'aws lambda update-function-configuration --function-name "'"${FUNCTION_NAME}"'" --environment "${ENV_JSON}"'
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

特に `TABLE_NAME` が消えると既定値の `mail_send_log_events` へフォールバックする。この名前は AWS 上に存在しないため `ResourceNotFoundException` となり、**その間の検知は行われない**。`Errors` アラームでは気づけるが、落ちたオブジェクトのログは再処理されない。

手順3 と同じ方法で JSON を組み立てる。全変数が揃っていることを目視してから適用する。

```bash
export TARGET_KEY_PREFIX="${KEY_PREFIX}"
ENV_JSON="$(python3 -c 'import json,os; print(json.dumps({"Variables":{k:os.environ[k] for k in ("TABLE_NAME","TARGET_KEY_PREFIX","SLACK_WEBHOOK_URL") if os.environ.get(k)}}))')"
echo "${ENV_JSON}"
```

```bash
aws lambda update-function-configuration --function-name "${FUNCTION_NAME}" --environment "${ENV_JSON}"
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

`TABLE_NAME` が消えると既定値の `mail_send_log_events` へフォールバックし、AWS 上に存在しないテーブルを指して落ちる。全変数を手で書き直すのではなく、現在値を読んでマージするほうが安全である。

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

**これがもっとも影響が大きい。** `put-bucket-notification-configuration` はバケットの通知設定を全置換するため、1件を追加するつもりで他の環境・他チームの通知を消すことが起こりうる。消えてもエラーは出ず、その通知だけが無言で止まる。

手順は構築時とまったく同じである。**手順5-3 のマージ手順をそのまま実行すること。** 既存設定の退避、同一 `Id` の差し替え、適用前の目視、切り戻しまで含まれている。

プレフィックスを変更する場合は `KEY_PREFIX` を設定し直してから、手順5-3 を頭から実行する。

### D-4. 変更できないもの

| 項目 | 対応 |
| --- | --- |
| DynamoDB のキー構成（`EmailID` / `recordKey`） | 変更不可。付録A の手順でテーブルを作り直す |
| DynamoDB のテーブル名 | 変更不可。新しい名前で作り直し、Lambda の `TABLE_NAME` を D-2 の手順で更新する |
| Lambda の関数名 | 変更不可。作り直しとなる。ロググループ・アラーム名も関数名に紐づくため合わせて作り直す |

### D-5. 変更後の確認

環境変数・プレフィックス・通知設定のいずれを変えた場合も、**手順7 の動作確認を再実行する**。設定の反映を API で確認できても、経路全体が通っているかは実際にログを流さないと分からない。

設定変更の直後は、Lambda の実行環境が入れ替わるまでにわずかな時間差がある。モジュール読み込み時に読まれる `TABLE_NAME` や `TARGET_KEY_PREFIX` は、切り替わるまで旧値が使われることがある。**変更直後に動作確認して想定と違う場合は、数十秒おいて再実行する。**

---

## 付録E：動作確認が通らない場合の切り分け

手順7 でレコードが0件のとき、上から順に確認する。**構築時に実際に踏んだものを記載している。**

### E-1. まず Lambda が起動しているかを見る

```bash
aws logs tail "/aws/lambda/${FUNCTION_NAME}" --since 10m
```

| 状態 | 次に見るところ |
| --- | --- |
| 何も出ない | E-2（ロググループの有無） |
| `Received S3 event` が出るが検証ファイル以外 | E-3（通知設定の反映待ち） |
| 検証ファイルの `Received S3 event` が出て、その後エラー | E-4（IAM 権限） |
| `Matched successful mail send` まで出ている | 正常。DynamoDB の確認方法を見直す |

**ログのパターン検索には `aws logs tail` とローカルの `grep` を使うこと。** `aws logs filter-log-events --filter-pattern` は JSON 中の部分文字列に対して期待どおり動かないことがあり、起動しているのに0件と表示されて誤った判断につながる。

### E-2. ロググループが無い場合

ロググループは Lambda の初回実行時に自動作成される。存在しない場合、**実行されていない**か、**実行ロールにログ書き込み権限が無い**かのいずれかである。

```bash
aws logs describe-log-groups --log-group-name-prefix "/aws/lambda/${FUNCTION_NAME}" --query 'logGroups[].logGroupName' --output text
```

権限の有無を確認する。

```bash
aws iam list-attached-role-policies --role-name "${ROLE_NAME}" --query 'AttachedPolicies[].PolicyName' --output text
```

`AWSLambdaBasicExecutionRole` が無ければ手順2-1 を実行する。

実行されているかどうかは、ログに依存しないメトリクスで判定できる。

```bash
aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Invocations \
  --dimensions Name=FunctionName,Value="${FUNCTION_NAME}" \
  --start-time "$(date -u -d '30 minutes ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 60 --statistics Sum --query 'sort_by(Datapoints,&Timestamp)[].{t:Timestamp,n:Sum}' --output table
```

関数自体が動くかは、空イベントを直接渡せば切り分けられる。副作用はない。

```bash
aws lambda invoke --function-name "${FUNCTION_NAME}" --payload '{"Records":[]}' --cli-binary-format raw-in-base64-out /tmp/out.json && cat /tmp/out.json
```

`{"saved_successful_send_logs": 0, ...}` が返れば、関数の読み込みと実行は正常。ただし空イベントでは S3 も DynamoDB も呼ばないため、**それらの権限が正しいことの証明にはならない。**

### E-3. 検証ファイルのイベントだけ届かない場合

**通知設定の反映待ちである可能性が最も高い。** 設定変更から実際の配信開始まで数分かかる。API 上は即座に設定が読み出せるため、反映済みに見える点が紛らわしい。

判定方法は、**他のログソースのイベントが届いているかどうか**である。届いていれば配信自体は生きているため、設定を触らず数分待ってから検証ファイルを置き直す。

```bash
aws logs tail "/aws/lambda/${FUNCTION_NAME}" --since 10m | grep -o "log_source%3D[a-z-]*" | sort | uniq -c
```

S3 イベントはオブジェクト作成時にしか発火しない。**設定を変更する前に置いたオブジェクトは、遡って処理されない。** 必ず置き直すこと。

なお、S3 イベント中のオブジェクトキーは URL エンコードされている（`=` が `%3D`）。Lambda 側でデコードしてから扱っているため、ログには復号後のキーが出力される。

```text
イベント : service%3Dworker/env%3Dstaging/log_source%3Dlaravel-app/...
ログ出力 : service=worker/env=staging/log_source=laravel-app/...
```

### E-4. `AccessDenied` が出る場合

インラインポリシーが適用されていない可能性が高い。`put-role-policy` は成功時に何も出力しないため、実行し忘れに気づきにくい。

```bash
aws iam get-role-policy --role-name "${ROLE_NAME}" --policy-name detect-mail-duplicates --query 'PolicyDocument.Statement[].{Sid:Sid,Resource:Resource}' --output table
```

`NoSuchEntity` が返る場合は手順2-2 を実行する。ポリシーがある場合は `Resource` の値が実際のバケット名・プレフィックス・テーブル名と一致しているかを確認する。IAM の変更は反映に数秒かかるため、適用直後の検証は失敗することがある。

### E-5. 検証時に設定を変更しない

切り分け中に設定を変えると、**変更の反映待ちと元の問題が重なって判断できなくなる。** 今回の構築では、通知設定を何度も切り替えながら直後に検証したため、フィルタの有無が原因かどうかを長時間特定できなかった。

- 1回に変更するのは1箇所だけにする
- 変更したら数分待ってから検証する
- 検証用オブジェクトは毎回新しいキーで置く


---

## 付録F：Slack 通知とアラーム経路の検証

手順7 が確認するのは検知そのものである。本付録は、**検知したあとの通知とアラームが届くか**を確認するためのテストパターンをまとめる。

### F-1. 実施順序

順番に依存関係がある。理由は2つ。

1. **メトリクスフィルタは設定後に出力されたログにしか適用されない。** 手順6-2 を失敗テストより先に済ませる
2. **Webhook を本番値にしたあとで、わざと壊す作業をしたくない。** 失敗経路の検証を疎通確認より先に行う

| 順 | 内容 | 前提 |
| --- | --- | --- |
| 1 | SNS トピック作成 → 手順6-1 / 6-2 | — |
| 2 | F-6 失敗経路とアラーム | 手順6 完了 |
| 3 | F-5 通知されないこと | いつでも可 |
| 4 | Webhook 設定（付録C-3） | F-6 完了後 |
| 5 | F-3 疎通 / F-4 通知本文 | Webhook 設定済み |
| 6 | F-7 判定窓の境界 | いつでも可 |
| 7 | 後片付け（`verify-` オブジェクト削除） | — |

### F-2. 検証用ログの生成

手順7 のスクリプトを、EmailID ごとの行数を指定できる形にしたもの。`COUNTS` にカンマ区切りで行数を並べると、その数だけ EmailID を作る。

```bash
export ENV_NAME=staging        # 本番: production
export COUNTS="3,2"            # EmailID-A を3行、EmailID-B を2行

python3 - <<'PY' 2>/tmp/verify-ids.txt | gzip > /tmp/verify.log.gz
import json, os, sys, time
from datetime import datetime, timezone

now = datetime.now(timezone.utc); env = os.environ["ENV_NAME"]; base = int(time.time())
for i, n in enumerate(int(c) for c in os.environ["COUNTS"].split(",")):
    eid = f"9{base}{i:03d}"
    ctx = {"mail_object_type": "Notification", "mail_object_id": 0, "subject": "検証用",
           "scheduled_at": now.strftime("%Y-%m-%d %H:%M:%S"),
           "content": f"Email {eid} was successfully sent!"}
    msg = (f'[{now.strftime("%Y-%m-%d %H:%M:%S")}] {env}.INFO: '
           f'EvenSendEmails [{env}] success  sending email: {eid} '
           f'{json.dumps(ctx, ensure_ascii=False)}  \n')
    for _ in range(n):
        print(json.dumps({"date": now.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
                          "log_source": "laravel.app", "log": msg, "service": "worker",
                          "env": env, "instance_id": "i-verify0000000001"}, ensure_ascii=False))
    print(f"{eid} x{n}", file=sys.stderr)
PY

cat /tmp/verify-ids.txt
```

投入は手順7 と同じ。パーティション構造に沿わせ、ファイル名は `verify-` で始める。

```bash
Y=$(date -u +%Y); M=$(date -u +%m); D=$(date -u +%d); H=$(date -u +%H)
export VERIFY_DIR="${KEY_PREFIX}year=${Y}/month=${M}/day=${D}/hour=${H}"
aws s3 cp /tmp/verify.log.gz "s3://${LOG_BUCKET}/${VERIFY_DIR}/verify-$(date +%s).gz"
```

### F-3. 疎通

| # | 内容 | 期待結果 |
| --- | --- | --- |
| F-3-1 | Webhook 設定前に重複を発生させる | `SLACK_WEBHOOK_URL is not set; skip Slack notification.` / Slack には出ない |
| F-3-2 | Webhook 設定後に重複を発生させる | `Slack notification sent: HTTP 200` |
| F-3-3 | Slack 画面での着弾確認 | 意図したチャンネル。改行・絵文字が崩れていない |
| F-3-4 | 設定直後の環境変数確認 | `TABLE_NAME` `TARGET_KEY_PREFIX` `SLACK_WEBHOOK_URL` の**3つ全部**が入っている |

F-3-4 が事故防止の本体である。`--environment` は環境変数マップを全置換するため、`TABLE_NAME` が消えると既定値へフォールバックし、**STG の Lambda が本番テーブルへ書き込む**。エラーは出ない。詳細は付録C-3。

```bash
aws lambda wait function-updated-v2 --function-name "${FUNCTION_NAME}" && aws lambda get-function --function-name "${FUNCTION_NAME}" --query 'Configuration.Environment.Variables'
```

### F-4. 通知本文

通知本文には性質の異なる2つの数字が出る。取り違えていないかを実データで確認する。

- `検知した重複: N 件` … 重複した **EmailID の種類数**
- `最多の EmailID: M 件` … そのうち**最も多く出た ID の出現回数**（直近1時間のローリング）

| # | `COUNTS` | 期待する本文 | 何を確かめるか |
| --- | --- | --- | --- |
| F-4-1 | `2` | 重複 1 件 / 最多 2 件 | 基本形 |
| F-4-2 | `2,2` | 重複 **2** 件 / 最多 **2** 件 | 2つの数字が別物だと分かる |
| F-4-3 | `3,2` | 重複 2 件 / 最多 **3** 件 | 最多の選択が正しいか |
| F-4-4 | 2 × 300 ID | 重複 **300** 件 / 最多 2 件 | **本文長が F-4-1 と同じ**であること |

F-4-2 と F-4-3 を分けているのは、F-4-2 だけでは両方 `2` になり取り違えが見えないためである。

F-4-4 は、EmailID を列挙しない設計の根拠（Slack の文字数制限に当たらない）を実証する。重複が何件になっても本文の長さは変わらない。

```bash
export COUNTS="$(python3 -c 'print(",".join(["2"]*300))')"
```

600行で約2.5秒。タイムアウト120秒には十分収まる。

### F-5. 通知されないこと

| # | 投入内容 | 期待結果 |
| --- | --- | --- |
| F-5-1 | `COUNTS="1"` | 保存1件、`Duplicate detected` 無し、Slack 無し |
| F-5-2 | マッチしない行のみのファイル | `saved_successful_send_logs: 0` |
| F-5-3 | **対象外プレフィックスのキー** | `Skip out-of-scope object:` / S3 を読まない |

S3 イベント通知側にプレフィックスフィルタを設定していないため、キーの絞り込みは Lambda 側の `TARGET_KEY_PREFIX` チェックだけが担う（手順5-2）。F-5-3 はその確認である。

オブジェクトを置く必要はない。読む前に弾かれる経路なので、偽のイベントを直接渡せば足りる。ログバケットを汚さずに済む。

```bash
aws lambda invoke --function-name "${FUNCTION_NAME}" --cli-binary-format raw-in-base64-out \
  --payload '{"Records":[{"s3":{"bucket":{"name":"'"${LOG_BUCKET}"'"},"object":{"key":"service=api/env=staging/log_source=nginx/year=2026/month=01/day=01/hour=00/dummy.gz"}}}]}' \
  /tmp/skip.json && cat /tmp/skip.json
```

存在しないキーを指定しているため、`NoSuchKey` が出た場合はプレフィックス判定が効いていないことになる。

### F-6. 失敗経路とアラーム

| # | 設定する URL | 期待するログ | アラーム |
| --- | --- | --- | --- |
| F-6-1 | `https://slack-webhook-not-configured.invalid/test` | `MAIL_DUPLICATE_SLACK_FAILED ...` | `-slack-failed` が ALARM |
| F-6-2 | `https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXX` | 同上（HTTP 404） | 同上 |
| F-6-3 | （変更なし）不正ペイロードで関数を落とす | `KeyError: 'Records'` | `-errors` が ALARM |

**F-6-2 を分けている理由。** `.invalid` は名前解決の段階で失敗するため、「Slack へ到達できる経路があるか」は何も証明しない。実在ホストに無効なパスで投げると TLS 確立まで進んで HTTP 404 になり、到達性と失敗ハンドリングを同時に確認できる。`HTTPError` は `URLError` の派生なので同じ経路で捕捉される。

URL の設定方法は付録C-2 を参照。設定のたびに環境変数の全置換が起きるため、毎回 F-3-4 の確認を行う。

F-6-3 は設定を一切変更せずに `Errors` を立てられる。

```bash
aws lambda invoke --function-name "${FUNCTION_NAME}" --cli-binary-format raw-in-base64-out --payload '{}' /tmp/err.json && cat /tmp/err.json
```

`FunctionError: Unhandled` が返れば `Errors` が1加算される。

```bash
aws cloudwatch describe-alarms --alarm-names "${FUNCTION_NAME}-slack-failed" "${FUNCTION_NAME}-errors" --query 'MetricAlarms[].{Name:AlarmName,State:StateValue,Reason:StateReason}'
```

評価期間が5分のため、ALARM へ変わるまで5〜10分かかる。`treat-missing-data notBreaching` を指定しているので、その後データが来なければ自動で OK へ戻る。手動リセットは不要。

### F-7. 判定窓の境界

1時間待つ必要はない。`createdAt` は Lambda 実行時刻で決まるためログ側では古くできないが、**DynamoDB へ直接古いレコードを置けば**境界を数分で確認できる。

| # | 事前に置くレコード | 投入 | 期待結果 |
| --- | --- | --- | --- |
| F-7-1 | 61分前 | 同一 ID × 1行 | count=1、**通知されない**（窓外が除外される） |
| F-7-2 | 59分前 | 同一 ID × 1行 | count=2、**通知される**（窓内が含まれる） |

```bash
export EMAIL_ID="9$(date +%s)"
export AGE_MIN=61              # F-7-2 では 59

OLD=$(python3 -c "from datetime import datetime,timedelta,timezone;import os;print((datetime.now(timezone.utc)-timedelta(minutes=int(os.environ['AGE_MIN']))).isoformat(timespec='microseconds'))")

aws dynamodb put-item --table-name "${TABLE_NAME}" --item "{\"EmailID\":{\"S\":\"${EMAIL_ID}\"},\"recordKey\":{\"S\":\"${OLD}#verify-window-boundary#1\"},\"createdAt\":{\"S\":\"${OLD}\"},\"expiresAt\":{\"N\":\"$(($(date +%s)+3600))\"}}"
```

そのうえで、F-2 のスクリプトの `eid` をこの `EMAIL_ID` に固定し、`COUNTS="1"` で投入する。

**F-7-1 と F-7-2 で EmailID を使い回さないこと。** 61分前のレコードが残ったまま F-7-2 を行うと件数が噛み合わなくなる。

### F-8. 連続検知時の通知量

運用上の通知ノイズを測る。同一 EmailID を含むファイルを、時間をあけて3回投入する。

| 回 | count | 通知本文 |
| --- | --- | --- |
| 1回目（2行） | 2 | 重複 1 件 / 最多 2 件 |
| 2回目（1行） | 3 | 重複 1 件 / 最多 **3** 件 |
| 3回目（1行） | 4 | 重複 1 件 / 最多 **4** 件 |

追記型のため、**1件の重複事象に対して後続オブジェクトのたびに通知が飛ぶ。** 本番のログ出力間隔が10分であれば、暴走が1時間続くと同じ EmailID で最大6回通知される。

通知の抑止は現設計に含まれていない。ここは「そういう挙動である」ことを記録する対象であり、許容できないと判断した場合に次段階の課題となる。

---

## 付録G：本番ログによるリプレイと負荷確認

本番から取得した実オブジェクトを1件通す。合成ログでは作れない要素（実際の行の混在比率、1行あたりのサイズ、コンテキスト JSON の実データ）をまとめて確認できる。

### G-1. 位置づけ

負荷確認であると同時に**バックテスト**でもある。実オブジェクトへ検知処理を通せば、「その時間帯に実際に重複があったか」が副産物として得られる。

**実施は Webhook を設定する前が望ましい。** 本番データに実重複が含まれていた場合、チャンネル用意直後に実インシデント由来の通知が飛び、テストか本物かの区別がつかなくなる。設定前であれば通知内容は CloudWatch Logs に出力される。

### G-2. 期待値を先に出す

Lambda の戻り値と突き合わせる独立した期待値を、手元で作っておく。これを飛ばすと「動いた」以上のことが言えない。

```bash
gunzip -c /tmp/prod-sample.gz | wc -l                                 # 総行数
gunzip -c /tmp/prod-sample.gz | wc -c                                 # 展開後サイズ
gunzip -c /tmp/prod-sample.gz | grep -cE 'success +sending email:'    # マッチ想定行数
```

EmailID ごとの出現分布まで出すと、重複件数の期待値がそのまま得られる。

```bash
gunzip -c /tmp/prod-sample.gz | grep -oE 'success +sending email: *[0-9]+' | grep -oE '[0-9]+$' | sort | uniq -c | sort -rn | head -20
```

- **2以上の行数** → `duplicate_email_ids` の期待値
- **先頭の件数** → Slack 本文「最多の EmailID」の期待値

### G-3. 投入

配置先は STG のパーティション配下、現在の UTC 時間。**`.gz` 拡張子は維持する**（コードが拡張子で展開要否を判定している）。

```bash
Y=$(date -u +%Y); M=$(date -u +%m); D=$(date -u +%d); H=$(date -u +%H)
export VERIFY_DIR="${KEY_PREFIX}year=${Y}/month=${M}/day=${D}/hour=${H}"
aws s3 cp /tmp/prod-sample.gz "s3://${LOG_BUCKET}/${VERIFY_DIR}/verify-replay-$(date +%s).gz"
```

### G-4. 測定項目

```bash
aws logs tail "/aws/lambda/${FUNCTION_NAME}" --since 10m --filter-pattern '"REPORT RequestId"'
```

| 項目 | 取得元 | 上限 | 見方 |
| --- | --- | --- | --- |
| Duration | REPORT 行 | 300,000 ms | 行数 × 10.5ms（1024MB での実測）と合うか |
| Max Memory Used | REPORT 行 | 1024 MB | 行単位のストリーム処理なので行サイズに依存。合成ログでは 102MB |
| `saved_successful_send_logs` | 戻り値 | — | G-2 の grep 行数と一致するか |
| `duplicate_email_ids` | 戻り値 | — | G-2 で2以上だった ID 数と一致するか |
| スロットル | 下記メトリクス | 0 | 0 以外なら要調査 |

```bash
aws cloudwatch get-metric-statistics --namespace AWS/DynamoDB --metric-name WriteThrottleEvents \
  --dimensions Name=TableName,Value="${TABLE_NAME}" \
  --start-time "$(date -u -d '15 minutes ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 60 --statistics Sum --query 'Datapoints[].Sum'
```

`WriteThrottleEvents` を `ReadThrottleEvents` に替えて読み取り側も確認する。

スロットルが起きると boto3 が内部で再試行し、Duration が跳ねる。再試行を使い切ると例外となり、Lambda 再試行は無効であるため**そのオブジェクトは丸ごと欠測する**。Duration が推定より大幅に長い場合は、まずここを疑う。ログに `ProvisionedThroughputExceededException` が出ていないかも確認する。

### G-5. 判定

| 結果 | 意味 |
| --- | --- |
| 保存件数が grep と一致、Duration に余裕 | 合格 |
| 保存件数が grep より少ない | 正規表現の行頭一致が本番ログの形と合っていない。該当行を特定する |
| 保存件数が grep より多い | 件名などに `success sending email:` を含む行を拾っている |
| Duration が 120秒に近い | タイムアウト引き上げ、またはスロットルの解消が必要 |

### G-6. 注意点

**同じオブジェクトを1時間以内に再投入しない。** 判定窓は投入時刻を基準とするため、2回目では全 EmailID が count=2 となり、**全件が重複として検知される**。やり直す場合は別のオブジェクトを使うか、1時間空ける。

**複数オブジェクトを続けて流す場合は、元ログが実際に連続した時間帯のものに限る。** `createdAt` は Lambda 実行時刻であるため、何時間も離れたオブジェクトを連投すると1つの窓に圧縮され、実在しない重複が出る。同一時間帯の連続オブジェクトであれば実運用と同じ条件になるので、そちらは有効なテストである。

**`rawLog` には本番の件名が入る。** STG のテーブルに1時間保存される。同一アカウント内かつログバケットと同じデータ区分だが、認識しておくこと。

**後片付けは必須。** 本番ログを STG パーティションへ置いた状態のため、集計対象に入る前に削除する。

```bash
aws s3 ls "s3://${LOG_BUCKET}/${VERIFY_DIR}/" | awk '/verify-/{print $4}'
```

### G-7. 極端系：同一 EmailID が大量に並ぶ場合

正常な本番ログでは出ない負荷パターンが1つある。**同一 EmailID が大量に並ぶケース**、すなわちこの仕組みが検知対象としている暴走ループそのものである。

`_count_recent` は行ごとに「その EmailID の直近1時間ぶん」を強整合で数え直す。同一 ID が n 行あると走査量は **n²/2 に比例** し、しかも同一パーティションキーであるため単一パーティションへ集中する。

800行すべてが同一 ID の場合、読み取り量は概算で 8万 RCU 相当が数秒に集中し、パーティションあたりの上限 3,000 RCU/s を超える。**検知すべき事象が起きたときにこそ検知処理が詰まる**可能性がある。

F-2 のスクリプトで再現できる。

```bash
export COUNTS="800"            # 1つのEmailIDが800行
```

見るのは Duration とスロットルの2つ。詰まる場合は、件数取得に上限を設けて「100件以上」と丸める方向の対処が考えられる。

これは**段階導入の判断材料であり、本番投入の前提条件ではない。** 通常の本番データでは発生しない条件である。
