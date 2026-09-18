# OAメール多重送信検知：運用コマンド集

日常の確認と調査で使うコマンドをまとめる。構築そのものは「OAメール多重送信検知_構築手順.md」を参照。

---

## 変数

**すべてのコマンドはこの変数を前提とする。** CloudShell のセッションごとに貼り直す。

```bash
export AWS_REGION=ap-northeast-1
export ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# 本番
export ENV_PREFIX=prd_
export ENV_SUFFIX=-prd
export KEY_PREFIX="service=worker/env=production/log_source=laravel-app/"

# STG に切り替える場合は上の3行を次に置き換える
#   export ENV_PREFIX=stg_
#   export ENV_SUFFIX=-stg
#   export KEY_PREFIX="service=worker/env=staging/log_source=laravel-app/"

export TABLE_NAME="${ENV_PREFIX}mail_send_log_events"
export FUNCTION_NAME="detect-mail-duplicates${ENV_SUFFIX}"
export ROLE_NAME="detect-mail-duplicates-role${ENV_SUFFIX}"
export METRIC_NAMESPACE="MailDuplicateDetector${ENV_SUFFIX}"
export LOG_BUCKET="hkz-log-archive"
export ALARM_TOPIC_ARN="arn:aws:sns:${AWS_REGION}:${ACCOUNT_ID}:detect-mail-duplicates-alarm${ENV_SUFFIX}"

echo "FUNC=${FUNCTION_NAME} / TABLE=${TABLE_NAME} / PREFIX=${KEY_PREFIX}"
```

> **STG は現在停止中。** S3 通知エントリを本番へ譲ったため、イベントを受け取らない。

---

## 1. 動いているかを見る

### 1-1. まとめて確認

```bash
echo "--- Lambda ---"
aws lambda get-function --function-name "${FUNCTION_NAME}" --query 'Configuration.{Mem:MemorySize,Timeout:Timeout,Env:Environment.Variables}' | sed -E 's#(hooks\.slack\.com/services/)[^"]*#\1***#'
echo "--- リトライ ---"
aws lambda get-function-event-invoke-config --function-name "${FUNCTION_NAME}" --query 'MaximumRetryAttempts'
echo "--- テーブル ---"
aws dynamodb describe-table --table-name "${TABLE_NAME}" --query 'Table.TableStatus' --output text
echo "--- S3通知 ---"
aws s3api get-bucket-notification-configuration --bucket "${LOG_BUCKET}" --query 'LambdaFunctionConfigurations[].{Id:Id,Prefix:Filter.Key.FilterRules[0].Value}'
echo "--- アラーム ---"
aws cloudwatch describe-alarms --alarm-name-prefix "${FUNCTION_NAME}" --query 'MetricAlarms[].{Name:AlarmName,State:StateValue,Actions:AlarmActions[0]}' --output table
```

### 1-2. 起動回数の推移

```bash
aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Invocations \
  --dimensions Name=FunctionName,Value="${FUNCTION_NAME}" \
  --start-time "$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 3600 --statistics Sum \
  --query 'sort_by(Datapoints,&Timestamp)[].{t:Timestamp,n:Sum}' --output table
```

ゼロの時間帯があれば、S3 イベントが届いていない。**ログに依存しないので、ロググループが無くても使える。**

### 1-3. エラーとスロットル

```bash
for m in Errors Throttles Duration; do
  echo "=== ${m} ==="
  aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name "${m}" \
    --dimensions Name=FunctionName,Value="${FUNCTION_NAME}" \
    --start-time "$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ)" \
    --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --period 3600 --statistics Sum,Maximum \
    --query 'sort_by(Datapoints,&Timestamp)[].{t:Timestamp,sum:Sum,max:Maximum}' --output table
done
```

`Duration` の `Maximum` がタイムアウト（300,000ms）に近づいていないかを見る。

---

## 2. DynamoDB の中身を見る

> **本文重複検知の稼働後は、専用テーブルが1つ増える。**
> 本章のコマンドは既存テーブル（`${TABLE_NAME}`）に対するもので、内容は変わらない。
>
> | テーブル | 用途 | パーティションキー |
> | --- | --- | --- |
> | `${ENV_PREFIX}mail_send_log_events` | EmailID 重複 | `EmailID` |
> | `${ENV_PREFIX}mail_send_log_events_by_content` | 本文重複 | `detectionKey` |
>
> 新テーブル向けのコマンドは、稼働時にこの章へ追記する。


### 2-1. 件数だけ

```bash
aws dynamodb scan --table-name "${TABLE_NAME}" --select COUNT --query 'Count'
```

読み取り量が少ない。まずこれ。

### 2-2. EmailID の一覧（出現回数つき）

```bash
aws dynamodb scan --table-name "${TABLE_NAME}" --projection-expression EmailID --query 'Items[].EmailID.S' --output text | tr '\t' '\n' | sort | uniq -c | sort -rn
```

先頭の数字が出現回数。**2以上が重複。**

### 2-3. 判定窓（直近1時間）に絞る

```bash
CUTOFF=$(python3 -c 'from datetime import datetime,timedelta,timezone;print((datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(timespec="microseconds"))')

aws dynamodb scan --table-name "${TABLE_NAME}" --projection-expression EmailID \
  --filter-expression 'createdAt >= :c' \
  --expression-attribute-values "{\":c\":{\"S\":\"${CUTOFF}\"}}" \
  --query 'Items[].EmailID.S' --output text | tr '\t' '\n' | sort | uniq -c | sort -rn
```

**TTL の削除は最大48時間遅れる**ため、2-2 には窓の外のレコードが混ざりうる。Lambda の判定件数と突き合わせるならこちら。`CUTOFF` は毎回計算し直すこと。

### 2-4. 重複しているものだけ

```bash
aws dynamodb scan --table-name "${TABLE_NAME}" --projection-expression EmailID --query 'Items[].EmailID.S' --output text | tr '\t' '\n' | sort | uniq -d
```

何も出なければ重複なし。

### 2-5. 1つの EmailID の詳細

```bash
export CHECK_ID=18929861

aws dynamodb query --table-name "${TABLE_NAME}" \
  --key-condition-expression 'EmailID = :id' \
  --expression-attribute-values "{\":id\":{\"S\":\"${CHECK_ID}\"}}" \
  --consistent-read \
  --query 'Items[].{at:createdAt.S,host:sourceHost.S,cmd:command.S,line:sourceLineNumber.N,src:sourceKey.S}' --output table
```

`host` が異なれば別インスタンスでの二重起動、同じなら同一インスタンス内での二重送信。

### 2-6. 保存されている生ログ

```bash
aws dynamodb query --table-name "${TABLE_NAME}" \
  --key-condition-expression 'EmailID = :id' \
  --expression-attribute-values "{\":id\":{\"S\":\"${CHECK_ID}\"}}" \
  --consistent-read --query 'Items[].rawLog.S' --output text
```

S3 へ行かずに原本の行を確認できる。

### 2-7. ID の歯抜けについて

**新しい側が歯抜けに見えるのは正常。** `EvenSendEmails` は偶数 ID、`OddSendEmails` は奇数 ID を担当し、別インスタンスで動く。Fluent Bit のフラッシュ時刻がずれるため、片方が約10分遅れて追いつく。

| 状態 | 判定 |
| --- | --- |
| 欠けが先端にだけある | 正常 |
| 欠けが奥に留まり続ける | 要調査（4-6 で原本を確認） |

---

## 3. Lambda のログを見る

> **`aws logs filter-log-events --filter-pattern` は JSON 中の部分文字列に対して期待どおり動かない。** `aws logs tail` とローカルの `grep` を使うこと。

### 3-1. 直近の動き

```bash
aws logs tail "/aws/lambda/${FUNCTION_NAME}" --since 10m | cut -c1-160
```

### 3-2. 検知と通知だけ

```bash
aws logs tail "/aws/lambda/${FUNCTION_NAME}" --since 1h | grep -E "Duplicate detected|Slack notification|MAIL_DUPLICATE"
```

### 3-3. 拾ったメールの件数

```bash
aws logs tail "/aws/lambda/${FUNCTION_NAME}" --since 1h | grep -c "Matched successful mail send"
```

### 3-4. 処理時間

```bash
aws logs tail "/aws/lambda/${FUNCTION_NAME}" --since 1h --filter-pattern '"REPORT RequestId"'
```

1行あたり約12.7ms（実測）。行数と照らして妥当かを見る。

### 3-5. どのオブジェクトで起動しているか

```bash
aws logs tail "/aws/lambda/${FUNCTION_NAME}" --since 30m | grep -o "log_source%3D[a-z-]*" | sort | uniq -c | sort -rn
```

S3 通知にフィルタを掛けていないため、対象外のログソースでも起動する。**約92%は対象外**（メールログは全体の約7.5%）。

### 3-6. リアルタイムで追う

```bash
aws logs tail "/aws/lambda/${FUNCTION_NAME}" --follow
```

---

## 4. S3 の生ログを見る

### 4-1. 最新のオブジェクト

```bash
Y=$(date -u +%Y); M=$(date -u +%m); D=$(date -u +%d); H=$(date -u +%H)
export BASE="${KEY_PREFIX}year=${Y}/month=${M}/day=${D}/hour=${H}/"
aws s3 ls "s3://${LOG_BUCKET}/${BASE}"
```

### 4-2. 中身の要約

```bash
LATEST=$(aws s3 ls "s3://${LOG_BUCKET}/${BASE}" | sort -k1,2 | tail -1 | awk '{print $4}')
echo "object: ${LATEST}"
aws s3 cp "s3://${LOG_BUCKET}/${BASE}${LATEST}" - --quiet | gunzip > /tmp/latest.log
echo "総行数    : $(wc -l < /tmp/latest.log)"
echo "送信成功行: $(grep -cE 'success +sending email:' /tmp/latest.log)"
echo "送信失敗行: $(grep -cE 'failed +sending email:' /tmp/latest.log)"
```

### 4-3. 送信ログ1件を整形して見る

> 本番ログには件名などの実データが含まれる。共有時は注意する。

```bash
grep -E 'success +sending email:' /tmp/latest.log | tail -1 | python3 -c '
import sys, json
d = json.loads(sys.stdin.read())
print("host   :", d.get("instance_id"))
print("date   :", d.get("date"))
print("raw    :", d["log"].strip())
i = d["log"].find("{")
if i >= 0:
    try:
        for k, v in json.loads(d["log"][i:].strip()).items():
            print(f"  {k:18s}: {v}")
    except Exception:
        pass
'
```

### 4-4. 時間帯ごとの送信数

```bash
check_day() {
  local DAY=$1
  for H in $(seq -w 0 23); do
    BASE="${KEY_PREFIX}year=$(date -u +%Y)/month=$(date -u +%m)/day=${DAY}/hour=${H}/"
    KEYS=$(aws s3 ls "s3://${LOG_BUCKET}/${BASE}" 2>/dev/null | awk '{print $4}')
    [ -z "$KEYS" ] && continue
    S=0; F=0; N=0
    for k in $KEYS; do
      N=$((N+1))
      T=$(aws s3 cp "s3://${LOG_BUCKET}/${BASE}${k}" - --quiet 2>/dev/null | gunzip 2>/dev/null)
      S=$((S + $(printf '%s\n' "$T" | grep -cE 'success +sending email:')))
      F=$((F + $(printf '%s\n' "$T" | grep -cE 'failed +sending email:')))
    done
    printf "day=%s hour=%s UTC  objects=%3d  success=%5d  failed=%3d\n" "${DAY}" "${H}" "${N}" "${S}" "${F}"
  done
}

check_day $(date -u +%d)
```

オブジェクト数は **12件/時**が平常値。`failed` は通常ゼロ。

### 4-5. Even / Odd のバランス

> **S3 のパーティション（`hour=`）は Fluent Bit が取り込んだ時刻であり、ログが出力された時刻ではない。**
> Even と Odd は別インスタンスで動き、フラッシュのタイミングが違うため、パーティション単位で数えると境界付近が片側へ寄る。
> **必ずログ行の中の時刻で集計すること。**

```bash
Y=$(date -u +%Y); M=$(date -u +%m); D=$(date -u +%d)

for H in 03 04 05; do   # 見たい時間の前後1時間を含める
  BASE="${KEY_PREFIX}year=${Y}/month=${M}/day=${D}/hour=${H}/"
  aws s3 ls "s3://${LOG_BUCKET}/${BASE}" 2>/dev/null | awk '{print $4}' | while read -r k; do
    aws s3 cp "s3://${LOG_BUCKET}/${BASE}${k}" - --quiet 2>/dev/null | gunzip 2>/dev/null
  done
done | grep -E 'success +sending email:' \
  | sed -E 's/.*\[([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}):[0-9]{2}:[0-9]{2}\] [^ ]+ (Even|Odd)SendEmails .*/\1 \2/' \
  | sort | uniq -c
```

```text
     53 2026-09-17 03 Even
     53 2026-09-17 03 Odd
    424 2026-09-17 04 Even
    423 2026-09-17 04 Odd
```

**担当は EmailID の偶奇で決まるため、本来はほぼ 1:1 になる。** 大きく崩れていれば、片方のコマンドが停止しているか、ID の採番に偏りがある。

パーティション単位で数えた場合、両者の差が数十件出ることがあるが、**前後の時間と合計すれば一致する**。それは異常ではない。

### 4-6. ログの出力時刻で集計する（一般形）

パーティションの時刻に頼らず、ログ行そのものの時刻で日付別に数える。**再送や遅延の検出にも使える。**

```bash
find <ログを展開したディレクトリ> -name '*.gz' -print0 | xargs -0 zcat 2>/dev/null \
  | grep -E 'success +sending email:' \
  | grep -oE '\[20[0-9]{2}-[0-9]{2}-[0-9]{2}' | tr -d '[' | sort | uniq -c
```

パーティションの日付と異なる日付が出た場合、**Fluent Bit がそのぶんを遅れて送っている**（実績あり。2026-09-15 に 9/10 のログ 4,666行が再送された）。

大量のオブジェクトを扱う場合は、1件ずつ `aws s3 cp` せず、まとめて落とすこと。

```bash
mkdir -p /tmp/logs
aws s3 cp "s3://${LOG_BUCKET}/${KEY_PREFIX}year=2026/month=09/day=15/" /tmp/logs/ --recursive
find /tmp/logs -name '*.gz' | wc -l
```

### 4-7. DynamoDB のレコードから原本の行へ

```bash
aws s3 cp "s3://${LOG_BUCKET}/<sourceKey の値>" - --quiet | gunzip | sed -n '<sourceLineNumber の値>p'
```

`<...>` はシェルのリダイレクトとして解釈されるため、**実際の値に置き換えてから実行する。**

### 4-8. ログ基盤が生きているか

```bash
Y=$(date -u +%Y);  M=$(date -u +%m);  D=$(date -u +%d);  H=$(date -u +%H)
PY=$(date -u -d '1 hour ago' +%Y); PM=$(date -u -d '1 hour ago' +%m)
PD=$(date -u -d '1 hour ago' +%d); PH=$(date -u -d '1 hour ago' +%H)

aws s3 ls "s3://${LOG_BUCKET}/service=worker/env=production/" | awk '{print $2}' | while read -r ls_; do
  last=$(for P in "year=${PY}/month=${PM}/day=${PD}/hour=${PH}/" "year=${Y}/month=${M}/day=${D}/hour=${H}/"; do
    aws s3 ls "s3://${LOG_BUCKET}/service=worker/env=production/${ls_}${P}" 2>/dev/null
  done | sort -k1,2 | tail -1 | awk '{print $1, $2}')
  printf "%-30s 最終書き込み: %s UTC\n" "${ls_}" "${last:-なし}"
done
echo "現在時刻: $(date -u +'%Y-%m-%d %H:%M:%S') UTC"
```

常時出力されるソース（`laravel-app` `laravel-horizon` `nginx-access` `system-cron` `eb-publish`）が10〜15分以内なら正常。`nginx-error` と `eb-engine` はエラー時のみのため「なし」でよい。

---

## 5. 重複を検知したときの調査

1. Slack 通知から EmailID を取る
2. `2-5` でレコードを引く。**`host` を見る**
3. `2-6` で生ログを見る、または `4-7` で原本を引く
4. `2-3` で他にも重複が出ていないかを確認する

| `host` | 示唆 |
| --- | --- |
| 2件で異なる | 別インスタンスが同じメールを担当した |
| 2件で同じ | 同一インスタンス内で二重に送信された |

`createdAt` の差は**検知時刻の差**であり、送信時刻の差ではない。送信時刻は生ログの行頭または `logTimestamp` を見る。

---

## 6. 設定を変更する

> 詳細と注意点は構築手順書「付録D：構築後に設定を変更する場合」を参照。

### 6-1. 環境変数（全置換される）

**`--environment` は環境変数マップ全体を置き換える。** 書き忘れた変数は消える。必ず全部を組み立てる。

```bash
export TARGET_KEY_PREFIX="${KEY_PREFIX}"
read -rsp "Slack Webhook URL: " SLACK_WEBHOOK_URL && export SLACK_WEBHOOK_URL && echo

ENV_JSON="$(python3 -c 'import json,os; print(json.dumps({"Variables":{k:os.environ[k] for k in ("TABLE_NAME","TARGET_KEY_PREFIX","SLACK_WEBHOOK_URL") if os.environ.get(k)}}))')"
echo "${ENV_JSON}" | sed -E 's#(hooks\.slack\.com/services/)[^"]*#\1***#'
```

3つ揃っていることを目視してから適用する。

```bash
aws lambda update-function-configuration --function-name "${FUNCTION_NAME}" --environment "${ENV_JSON}" > /dev/null \
  && aws lambda wait function-updated-v2 --function-name "${FUNCTION_NAME}" \
  && aws lambda get-function --function-name "${FUNCTION_NAME}" --query 'Configuration.Environment.Variables' | sed -E 's#(hooks\.slack\.com/services/)[^"]*#\1***#'
```

> `read` を含むブロックを複数行まとめて貼ると、次の行が入力として飲み込まれる。**1行ずつ実行すること。**

### 6-2. メモリ・タイムアウト（環境変数は保たれる）

```bash
aws lambda update-function-configuration --function-name "${FUNCTION_NAME}" --memory-size 1024 --timeout 300 > /dev/null \
  && aws lambda wait function-updated-v2 --function-name "${FUNCTION_NAME}" \
  && aws lambda get-function --function-name "${FUNCTION_NAME}" --query 'Configuration.{Mem:MemorySize,Timeout:Timeout}'
```

### 6-3. コードの更新

```bash
zip -j /tmp/detect-mail-duplicates.zip detect_mail_duplicates.py \
  && aws lambda update-function-code --function-name "${FUNCTION_NAME}" --zip-file fileb:///tmp/detect-mail-duplicates.zip > /dev/null \
  && aws lambda wait function-updated-v2 --function-name "${FUNCTION_NAME}" \
  && echo updated
```

環境変数・メモリ・タイムアウトは保たれる。

### 6-4. S3 通知設定

**全置換になる。必ず退避してからマージする。** 手順は構築手順書 5-3 をそのまま使う。

```bash
aws s3api get-bucket-notification-configuration --bucket "${LOG_BUCKET}" | jq 'del(.ResponseMetadata)' > ~/s3-notif-before.json
cat ~/s3-notif-before.json
```

**変更後は配信への反映に数分（実測7分）かかる。** その間に置いたオブジェクトはイベントを発火しない。

---

## 7. 動作確認

### 7-1. 配信経路だけを確認する（DynamoDB に書かない）

マッチする行を含まないファイルを置く。保存も通知も発生しない。

```bash
echo "probe line - no mail send log here" | gzip > /tmp/probe.gz
Y=$(date -u +%Y); M=$(date -u +%m); D=$(date -u +%d); H=$(date -u +%H)
aws s3 cp /tmp/probe.gz "s3://${LOG_BUCKET}/${KEY_PREFIX}year=${Y}/month=${M}/day=${D}/hour=${H}/verify-probe-$(date +%s).gz"
```

```bash
aws logs tail "/aws/lambda/${FUNCTION_NAME}" --since 3m | grep verify-probe
```

### 7-2. 検知と通知を通しで確認する

**本番で行う場合は、チャンネルへ事前に一声かけること。** EmailID は14桁（`9` + エポック秒）になるため、実データ（8桁）と区別できる。

```bash
export ENV_NAME=production     # STG: staging
export COUNTS="2"              # EmailIDごとの行数。"3,2" なら2種類

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

```bash
Y=$(date -u +%Y); M=$(date -u +%m); D=$(date -u +%d); H=$(date -u +%H)
export VERIFY_DIR="${KEY_PREFIX}year=${Y}/month=${M}/day=${D}/hour=${H}"
aws s3 cp /tmp/verify.log.gz "s3://${LOG_BUCKET}/${VERIFY_DIR}/verify-$(date +%s).gz"
```

```bash
aws logs tail "/aws/lambda/${FUNCTION_NAME}" --since 3m | grep -E "Duplicate detected|Slack notification|MAIL_DUPLICATE"
```

**同じテストを1時間以内に繰り返すと、前回のレコードが窓に残る。** EmailID は毎回変わるので通常は問題ないが、件数を検証する場合は注意する。

### 7-3. 関数が動くかだけを見る（副作用なし）

```bash
aws lambda invoke --function-name "${FUNCTION_NAME}" --payload '{"Records":[]}' --cli-binary-format raw-in-base64-out /tmp/out.json && cat /tmp/out.json
```

S3 も DynamoDB も呼ばないため、**権限が正しいことの証明にはならない。**

### 7-4. 後片付け

```bash
aws s3 ls "s3://${LOG_BUCKET}/service=worker/" --recursive | awk '/verify-/{print $4}'
```

一覧を目視してから削除する。

```bash
aws s3 ls "s3://${LOG_BUCKET}/service=worker/" --recursive | awk '/verify-/{print $4}' | while read -r k; do
  aws s3 rm "s3://${LOG_BUCKET}/${k}"
done
```

DynamoDB のレコードは TTL で消えるため不要。

---

## 8. 切り分け

検知されない、通知が来ない、といった場合の順序。詳細は構築手順書「付録E」。

| 症状 | 最初に見るもの |
| --- | --- |
| 検知されない | `1-2` 起動回数。ゼロなら S3 通知の問題 |
| 起動しているが保存されない | `3-1` のログ。`Skip out-of-scope` ならプレフィックス、`AccessDenied` なら IAM |
| 保存されるが通知されない | `3-2`。`SLACK_WEBHOOK_URL is not set` なら環境変数 |
| 通知が失敗する | `3-2` の `MAIL_DUPLICATE_SLACK_FAILED`。URL かネットワーク |
| 設定を変えた直後に動かない | **反映待ち。** 5分待ってから新しいオブジェクトで再確認 |

```bash
aws iam get-role-policy --role-name "${ROLE_NAME}" --policy-name detect-mail-duplicates --query 'PolicyDocument.Statement[].{Sid:Sid,Resource:Resource}' --output table
aws iam list-attached-role-policies --role-name "${ROLE_NAME}" --query 'AttachedPolicies[].PolicyName' --output text
```

`NoSuchEntity` が返る、または `AWSLambdaBasicExecutionRole` が無ければ、構築手順書 2-1 / 2-2 を実行する。

---

## 9. STG を再開する場合

### 9-1. 残っているもの・足りないもの

本番導入時に S3 通知エントリを本番へ譲ったため、STG は停止している。リソース自体は残っている。

| リソース | 状態 |
| --- | --- |
| Lambda `detect-mail-duplicates-stg` | 残っている。起動していないため課金なし |
| DynamoDB `stg_mail_send_log_events` | 残っている。TTL で空（0件） |
| IAM ロール | 残っている |
| CloudWatch アラーム2つ | 残っている。**通知先（SNS）が未設定** |
| ロググループ | 残っている。保持期間30日 |
| **S3 通知エントリ** | **削除済み。これが唯一の欠けている要素** |

### 9-2. 再開すると本番が止まる

**S3 は同一バケットで条件が重なる通知設定を許可しない。** フィルタなしのエントリは2つ登録できないため、STG を戻すには本番のエントリを外すことになる（構築手順書 5-2）。

取れる選択肢は3つ。

| 方法 | 可否 |
| --- | --- |
| 本番を止めて STG に戻す | 可能だが本番の検知が止まる |
| プレフィックスフィルタで両立 | **フィルタが効かない問題が未解決**。解決すれば可能 |
| STG 用に別バケットを用意する | 可能。ログ出力先の変更が必要 |

**現実的には、フィルタの原因を特定するのが先である。** イベント中のキーが URL エンコードされている（`service%3Dworker/...`）ため、フィルタ側も同じ形で評価されている可能性がある。未検証。

### 9-3. 検証だけならフィルタを試せる

本番を止めずに確認する方法はある。**STG のエントリを URL エンコード形式のフィルタ付きで追加する。** 本番はフィルタなしのままなので、文字列としては重ならず、登録は通る。

```bash
aws s3api get-bucket-notification-configuration --bucket "${LOG_BUCKET}" | jq 'del(.ResponseMetadata)' > ~/s3-notif-before.json
cat ~/s3-notif-before.json
```

```bash
cat > ~/s3-notif-stg-entry.json <<ENTRY
{
  "Id": "detect-mail-duplicates-stg",
  "LambdaFunctionArn": "arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:detect-mail-duplicates-stg",
  "Events": ["s3:ObjectCreated:*"],
  "Filter": { "Key": { "FilterRules": [ { "Name": "prefix", "Value": "service%3Dworker/env%3Dstaging/log_source%3Dlaravel-app/" } ] } }
}
ENTRY

jq --slurpfile e ~/s3-notif-stg-entry.json '
  .LambdaFunctionConfigurations =
    (((.LambdaFunctionConfigurations // []) | map(select(.Id != $e[0].Id))) + [$e[0]])
' ~/s3-notif-before.json > ~/s3-notif-after.json

jq -r '.LambdaFunctionConfigurations[] | "\(.Id)  →  \(.Filter.Key.FilterRules[0].Value // "(フィルタなし)")"' ~/s3-notif-after.json
```

本番のエントリが残っていることを確認してから適用する。

```bash
aws s3api put-bucket-notification-configuration --bucket "${LOG_BUCKET}" --notification-configuration "file://${HOME}/s3-notif-after.json"
```

**適用から5分以上待ってから**、STG のプレフィックスへ probe を置いて確認する（7-1 を STG のプレフィックスで実行）。

| 結果 | 判定 |
| --- | --- |
| STG の Lambda が起動する | **エンコード形式が正解。** 両環境をフィルタで分離できる |
| 起動しない | 仮説が外れ。別の原因を探す |

どちらの場合も、確認後は退避したファイルで元に戻せる。

```bash
aws s3api put-bucket-notification-configuration --bucket "${LOG_BUCKET}" --notification-configuration "file://${HOME}/s3-notif-before.json"
```

> **本番のエントリには触れないこと。** この検証で追加・削除するのは STG のエントリだけである。

### 9-4. 再開時に必要な残作業

STG を本格的に戻す場合、通知先の扱いを決める必要がある。

- アラームに SNS の通知先が設定されていない（構築時に意図的に省略した）
- `SLACK_WEBHOOK_URL` は未設定。**Slack チャンネルが本番と兼用のため、意図的にそうしている**。STG の通知を本番チャンネルへ流すと、実障害との区別がつかなくなる
