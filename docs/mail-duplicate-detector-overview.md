# OAメール送信側重複検知: 現状整理

現行の設計仕様は [OAメール多重送信検知_送信側重複_最小修正版.md](OAメール多重送信検知_送信側重複_最小修正版.md)、本番の構築手順は [OAメール多重送信検知_構築手順.md](OAメール多重送信検知_構築手順.md) を参照する。

`OAメール多重送信検知_送信側重複_修正版.md` は `recordId` 主キーと GSI 2本を前提とした旧版であり、採用していない。

## 目的

メール送信成功ログに同じ `EmailID` が短時間に複数回現れたとき、送信側の重複を検知する。ローカル環境で AWS に近い配送経路と通知までを再現し、STG 導入前に仕様と実装を検証できる状態にする。

現時点の方針は、見逃しより通知過多を優先すること。1時間内に同じ `EmailID` がさらに検出されれば、追加でも通知する。

## 構成

```text
ログ（Fluent Bit JSON Lines / 平文 / gzip）
  → S3: mail-send-logs
  → S3 ObjectCreated イベント
  → Lambda: detect-mail-duplicates
       ├→ DynamoDB Local: mail_send_log_events
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
| 重複判定 | Lambda が検知した時刻から直近1時間に同じ `EmailID` が2件以上。ベーステーブルの `Query`（`ConsistentRead=true`）で数える。GSI は使わない |
| 履歴 | `mail_send_log_events` にログ1行ごとに1レコード保存。PK=`EmailID`、SK=`{createdAt}#{sourceKey}#{行番号}`。`logTimestamp`、コマンド、S3入力元、生ログを保持 |
| 通知 | `SLACK_WEBHOOK_URL` があれば JSON の `{"text":"..."}` を POST。本文は今回検知した重複件数と `EmailID` あたりの最多件数。未設定なら Lambda ログのみ |
| ローカル通知先 | 起動時に `always-success` の疑似 Slack URL を環境変数へ自動設定 |
| Slack エラー | ログへ記録するが、検知 Lambda 自体は失敗にしない |

## 主なファイル

| 目的 | ファイル |
| --- | --- |
| 設計仕様 | `docs/OAメール多重送信検知_送信側重複_最小修正版.md` |
| 構築手順 | `docs/OAメール多重送信検知_構築手順.md` |
| **運用コマンド集** | `docs/OAメール多重送信検知_運用コマンド集.md` |
| 重複検知 Lambda | `localstack/init/ready.d/detect_mail_duplicates.py` |
| 疑似 Slack API Lambda | `localstack/init/ready.d/mock_slack_api.py` |
| LocalStack の自動構築 | `localstack/init/ready.d/01-bootstrap-fluentbit-sample.sh` |
| DynamoDB 履歴表示 | `client/sample/show_mail_duplicate_events.py` |
| 通常の検知テスト | `scripts/test-mail-duplicate-detector.sh` |
| 100件性能テスト | `scripts/benchmark-mail-duplicate-detector.sh` |
| 近接二重配送テスト | `scripts/test-mail-duplicate-concurrent.sh` |
| 同一ファイル内重複テスト | `scripts/test-mail-duplicate-same-file.sh` |

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
| 同一ファイル内重複 | `./scripts/test-mail-duplicate-same-file.sh` | 1オブジェクト内の同一 EmailID 2行が2レコードとして保存され、`createdAt` と `sourceKey` が同値であること、疑似 Slack 1回 | 自動削除 |
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

## 実測済みの結果（ローカル）

- 1つの S3 オブジェクト内に同じ `EmailID` を含む2行を置き、2レコードとして保存され重複として検知されることを確認済み。2件の `createdAt` と `sourceKey` が同値で、ソートキーの行番号だけが両者を分けていることも確認済み
- TTL 未削除の古いレコードを残した状態で、2時間前のレコードは窓外として除外され、30分前のレコードは窓内として検知されることを確認済み
- `failed sending email:` の行が保存・判定の対象外になることを gzip 入力で確認済み
- 同時配送（2オブジェクトを同時アップロード）を5回試行し、**5回とも検知漏れなし**。うち1回は双方の Lambda が検知して疑似 Slack が2回呼ばれた
- 処理時間はログ行数に対して線形で、**1行あたり約 4.2ms**（50行=211ms、100行=409ms、300行=1,314ms、600行=2,499ms）
- 疑似 Slack API の `200`、`500`、`500 → 500 → 200` を確認済み

ローカルの処理時間は AWS 本番のネットワーク遅延や Lambda コールドスタートを表すものではなく、ロジックの確認値として扱う。**性能見積もりには使えない。** STG での実測は同条件（600行）で 10.5ms/行 であり、8倍の開きがあった（後述）。

## STG環境での検証結果（2026-09-15）

> **現在 STG の検知は停止している。** 本番導入時に S3 通知エントリを本番へ付け替えたため、イベントが届かない。Lambda・テーブル・アラームは残っている。経緯は「S3 イベント通知の制約」を参照。以下は停止前に実施した検証の記録である。

STG へ構築し、検知する／検知しないの両方向を実データで確認した。構築手順は [OAメール多重送信検知_構築手順.md](OAメール多重送信検知_構築手順.md) を参照する。

### 検証環境

| 項目 | 値 |
| --- | --- |
| DynamoDB テーブル | `stg_mail_send_log_events`（PK=`EmailID` / SK=`recordKey` / TTL=`expiresAt`） |
| Lambda 関数 | `detect-mail-duplicates-stg`（python3.12 / 120秒 / 256MB / リトライ無効） |
| ログバケット | `hkz-log-archive` |
| 対象プレフィックス | `service=worker/env=staging/log_source=laravel-app/` |
| S3 イベント通知 | **フィルタなし**。対象の絞り込みは Lambda 側の `TARGET_KEY_PREFIX` で実施 |
| Slack | 未設定（通知本文は CloudWatch Logs へ出力） |

STG では送信対象のメールが常に0件（`Total emails to be sent in staging: 0` が毎分出力される）ため、実際の送信成功ログは発生しない。検証は本番と同形式の合成ログで行った。

### 検知されること

同一 S3 オブジェクト内に同じ `EmailID` を2行含むログを投入し、**6回すべてで検知・通知を確認**した。

```text
Matched successful mail send: {"EmailID": "91789460837", "command": "EvenSendEmails", "lineNumber": 1}
Matched successful mail send: {"EmailID": "91789460837", "command": "EvenSendEmails", "lineNumber": 2}
Duplicate detected: EmailID=91789460837 count=2
⚠️ メール重複を検知しました
検知した重複: 1 件
最多の EmailID: 2 件（EmailID: 91789460837）
```

保存されたレコードは、`createdAt` と `sourceKey` が同値で `sourceLineNumber` だけが異なる2件であり、`sourceHost` にはそれぞれ別のインスタンスIDが入る。**ソートキーに `sourceKey` と `sourceLineNumber` を含めた修正が意図どおり機能している**ことを示す。

### 検知されないこと

7行の混在ログを1オブジェクトとして投入し、**3行のみがマッチ**した。マッチした3件はいずれも `EmailID` が一意であり、`Duplicate detected` は出力されていない。

| 行 | 内容 | 結果 |
| --- | --- | --- |
| 1 | `EvenSendEmails [staging] success  sending email: <ID>` | マッチ（1件保存・通知なし） |
| 2 | `failed  sending email:` | 除外 |
| 3 | `OddSendEmails [staging] success  sending email: <ID>` | マッチ（1件保存・通知なし） |
| 4 | 通常のログ行（`array (...)`） | 除外 |
| 5 | `SomeOtherCommand [staging] success  sending email: <ID>` | 除外（対象外コマンド） |
| 6 | `staging.ERROR: EvenSendEmails ... success` | 除外（`INFO` のみ対象） |
| 7 | `SendEmails [staging] success  sending email: <ID>` | マッチ（1件保存・通知なし） |

DynamoDB 上の件数が、そのまま判定結果を表している。

```text
1 11789460754      ← 単発。通知しない
1 117894607549     ← 単発。通知しない
1 31789460754      ← 単発。通知しない
2 91789459635      ← 重複。通知した
2 91789459876      ← 重複。通知した
（以下、重複はすべて2件かつ通知あり）
```

「1件目は通知しない」「対象コマンド3種すべてでマッチする」「失敗ログとログレベル違いを除外する」がいずれも確認できた。

### 配信の安定性

S3 イベント通知の設定を変更せずに、合成ログを連続で5回投入し、**5回すべてが処理された**（取りこぼしなし）。

構築中に1件だけ未処理となったものがあるが、これは通知設定を変更した約70秒後に投入したもので、設定変更の反映待ちによるものと特定している。設定が安定した状態での欠落は観測されていない。

### 通知の内容と処理性能（2026-09-16）

Slack Webhook を設定し、通知経路を通しで確認した。

| テスト | 投入 | 結果 |
| --- | --- | --- |
| 疎通 | 同一 EmailID × 2行 | `Slack notification sent: HTTP 200`、チャンネル着弾 |
| 最多判定 | ID-A × 3行、ID-B × 2行 | 「検知した重複: 2 件 / 最多の EmailID: 3 件」 |
| 規模 | 300 ID × 各2行（600行） | 「検知した重複: 300 件 / 最多の EmailID: 2 件」。本文長は2行の場合と同一 |

「検知した重複（EmailID の種類数）」と「最多の EmailID（その ID の出現回数）」が別の値として正しく出ること、および**重複が何件あっても通知本文の長さが変わらない**ことを確認した。EmailID を列挙しない設計の根拠が満たされている。

投入から検知・通知までは約3秒だった。

**処理性能はローカル計測から大きく外れた。** 600行の同一ログでメモリ設定のみを変えて計測した結果は次のとおり。

| メモリ | Duration | 1行あたり |
| --- | --- | --- |
| 256 MB（当初値） | 20,110 ms | 33.5 ms |
| 1024 MB（変更後） | 6,320 ms | 10.5 ms |

メモリ使用量はどちらも 102MB で頭打ちしており、差は CPU とネットワーク帯域の配分による。1行につき DynamoDB への書き込みと強整合読み取りの2往復が発生するため、ネットワーク遅延が支配的である。

当初の 256MB・タイムアウト120秒では、ピーク流量（240通/分）で30分ぶんが滞留した場合にタイムアウトし、**そのオブジェクトが丸ごと検知対象から落ちる**見込みだった。再試行を無効にしているため復旧しない。**メモリ 1024MB・タイムアウト 300 秒へ変更**し、構築手順書にも反映済み。

### 未検証の項目

| 項目 | 備考 |
| --- | --- |
| 送信失敗時のアラーム | SNS トピック未用意のため、手順6 が未実施 |
| 1時間の判定窓の境界 | ローカルで検証済み。STGでは未実施 |
| 同時実行時の取りこぼし防止 | 同上 |
| ~~本番相当のログ流量~~ | **2026-09-16 実測済み**（「本番の実流量」を参照）。合成ログのリプレイではなく、本番の実オブジェクトで確認した |

### 構築時に判明した注意点

いずれも構築手順書の「付録E：動作確認が通らない場合の切り分け」に反映済み。

- S3 イベント通知の設定変更は、**実際の配信へ反映されるまで数分かかる**。API 上は即座に読み出せるため反映済みに見える
- `aws iam put-role-policy` は成功時に何も出力しないため、実行し忘れに気づけない。適用後の確認が必須
- ログ権限をインラインポリシーで自前定義すると `logs:CreateLogGroup` のリソース指定が合わず、**Lambda は実行されるのにログが残らない**。`AWSLambdaBasicExecutionRole` を使う
- `aws logs filter-log-events --filter-pattern` は JSON 中の部分文字列に対して期待どおり動かない。`aws logs tail` とローカルの `grep` を使う
- S3 イベント中のオブジェクトキーは URL エンコードされる（`=` が `%3D`）。Lambda 側でデコードしてから判定している

## 本番環境への導入（2026-09-16）

### 構成

| リソース | 名前 |
| --- | --- |
| DynamoDB テーブル | `prd_mail_send_log_events` |
| Lambda 関数 | `detect-mail-duplicates-prd`（メモリ 1024MB / タイムアウト 300秒 / 再試行 0） |
| IAM ロール | `detect-mail-duplicates-role-prd` |
| SNS トピック | `detect-mail-duplicates-alarm-prd`（メール購読・確認済み） |
| S3 通知 | `hkz-log-archive` に1件のみ。**フィルタなし** |
| 対象プレフィックス | `service=worker/env=production/log_source=laravel-app/`（Lambda の `TARGET_KEY_PREFIX`） |

環境の接頭辞・接尾辞は `prd_` / `-prd` とした。**当初案の「本番は空文字」は採用していない。** Lambda のコードは `TABLE_NAME` 未設定時に既定値 `mail_send_log_events` へフォールバックするため、本番を空文字で命名すると、設定漏れがそのまま本番テーブルへの書き込みになる。`prd_` を付けることで既定値は AWS 上に存在しない名前となり、設定が欠けた時点で `ResourceNotFoundException` で落ちる。沈黙するより落ちるほうがよい、という判断である。

### 実ログでの動作確認

投入した合成データではなく、**本番の実際のメール送信ログで確認した**。

```
Matched successful mail send: {"EmailID": "18929544", "command": "EvenSendEmails", ...}
Matched successful mail send: {"EmailID": "18929549", "command": "OddSendEmails", ...}
...計16件
REPORT Duration: 283.80 ms
```

| 確認項目 | 結果 |
| --- | --- |
| 実ログの検知 | 1オブジェクトで16件。`EvenSendEmails` / `OddSendEmails` 両方 |
| DynamoDB への保存 | 16件。ログのマッチ数と一致 |
| 処理時間 | 約12.7ms/行。STG実測の10.5ms と整合 |
| 対象外の除外 | `service=api` および `env=staging` のログを `Skip out-of-scope` で除外 |
| 重複の有無 | この時点では検知なし |

`service=api` 配下にも `log_source=laravel-app` のログが存在し、実際に除外されている。**プレフィックスを `service=api` にしていた場合、Lambda は動作するのにメール送信ログを1件も拾えなかった**ことになる。

### S3 イベント通知の制約

**1つのバケットに登録できる消費者は実質1つだけである。** これが構成上いちばん強い制約だった。

S3 は同一バケット・同一イベント種別で条件が重なる通知設定を許可しない（`Configurations on the same bucket cannot share a common event type`）。フィルタなしの設定同士は完全に重なるため、2件目の登録が拒否される。

環境ごとに分けるにはプレフィックスフィルタが必要になるが、**フィルタを付けると本番・STG のどちらにもイベントが届かなくなった**。適用から15分以上待っても変わらない。

```json
{ "Name": "Prefix", "Value": "service=worker/env=production/log_source=laravel-app/" }
```

イベント中のオブジェクトキーは URL エンコードされている（`service%3Dworker/...`）ため、フィルタ側も同じ形で評価されている可能性がある。**未検証の仮説である。**

本番稼働を優先し、STG の通知エントリを削除して本番のみフィルタなしで登録した。**STG は現在イベントを受け取らず、検知は停止している。** Lambda・テーブル・アラームは残っているため、通知エントリを付け替えれば再開できる。

### 本番の実流量（2026-09-16 実測）

導入が他機能へ影響していないかの確認として、導入前日（9/15）と当日（9/16）のログを突き合わせた。あわせて未検証だった「本番相当のログ流量」もここで確定した。

| 項目 | 実測値 |
| --- | --- |
| ログオブジェクト数 | **12件/時**。両日とも一定で、欠落した時間帯なし |
| 送信成功ログ | 0〜4,891件/時。時間帯による変動が大きいが、両日とも同じ振れ幅 |
| 送信失敗ログ | **全時間帯でゼロ** |
| 1オブジェクトあたり | 最大時間帯（4,891件/時 ÷ 14オブジェクト）で**約350行** |
| 推定処理時間 | 約4.4秒（350行 × 12.7ms）。タイムアウト300秒に対し60倍以上の余裕 |
| Even / Odd の比率 | 完了時間帯で均衡（396/397、169/168、50/46 など） |

**当初の見積もり「ピーク時2,400行/オブジェクト」を大きく下回った。** 送信数の変動は配信スケジュールによるもので、ピークが特定のオブジェクトに集中する形にはなっていない。容量面の懸念は解消した。

検知の遅延は、実測で**ログ出力から保存まで約1分47秒**だった。ただしこれはフラッシュ直前に出力された行であり、**上限はフラッシュ間隔（約10分）＋数秒**である。判定窓の1時間に対して十分小さい。

#### 他機能への影響

影響なしと確認した。根拠は次のとおり。

- アプリケーション・ワーカー・スケジューラには変更を加えていない。追加したのは S3 のログを読むだけの独立した仕組みで、**書き込み権限を持たない**
- 共有リソースで変更したのは `hkz-log-archive` の通知設定1箇所のみ。構築前は空であり、他チームの設定は存在しなかった
- 全ログソースが現在も書き込みを継続している（メール関連は確認時点で1〜2分前）
- 実際の送信ログ1件を DynamoDB のレコードと突き合わせ、EmailID・コマンド・ホストがすべて一致することを確認した

### 保存レコードを読むときの注意

DynamoDB の EmailID 一覧を見ると、**新しい側が必ず歯抜けに見える**。これは正常であり、取りこぼしではない。

```
544 ... 577          ← 連番。両方のコマンドのログが揃っている
579  581  583 ...    ← 先端。奇数のみ
```

`EvenSendEmails` は偶数 ID、`OddSendEmails` は奇数 ID を担当する。両者は別インスタンスで動き、それぞれの Fluent Bit が別のタイミングで S3 へ書き出すため、**片方が1フラッシュ分（約10分）遅れて追いつく**。数分後に数え直せば埋まる。

判断の基準は次のとおり。

| 状態 | 判定 |
| --- | --- |
| 欠けが先端にだけある | 正常。フラッシュ時刻のずれ |
| 欠けが奥に留まり続ける | 要調査。生ログを直接確認する |

検知への影響は無い。EmailID の偶奇で担当コマンドが決まるため、**1つの EmailID は必ず片方だけが扱う**。到着時刻がずれても同一 ID のレコードが分断されることはなく、ずれ幅（約10分）は判定窓の1時間に十分収まる。

なお、欠けている ID が生ログにどう出ているかで原因は切り分けられる。

| 生ログ | 意味 |
| --- | --- |
| `success sending email:` がある | **取りこぼし。** 要調査 |
| `failed sending email:` がある | 送信失敗。設計どおり除外している |
| 該当行が無い | まだ S3 に来ていないか、送信されていない |

**送られるべきものが送られていないことは、この仕組みの検知対象ではない。** 片方のコマンドが停止しても重複は発生しないため、アラームは鳴らない。

### 通知経路の本番疎通確認

本番の Lambda から Slack へ投稿した実績が無かったため、合成ログで1回だけ通した。実データと区別できるよう、EmailID は14桁（`9` + エポック秒）とした。本番の実 ID は8桁である。

```
⚠️ メール重複を検知しました
検知した重複: 1 件
最多の EmailID: 2 件（EmailID: 91789541397000）
検知日時: 2026-09-16 15:50:19 JST
```

```
Duplicate detected: EmailID=91789541397000 count=2
Slack notification sent: HTTP 200
```

**S3 → Lambda → DynamoDB → 判定 → Slack の全経路が本番で通った。** チャンネルが本番と兼用のため、実施前後に告知を投稿してから行った。検証用オブジェクトは削除済み。

### 本番で未確認の項目

| 項目 | 状態 |
| --- | --- |
| 実際の重複の検知 | 本番ではまだ重複が発生していない。仕組みの疎通は上記で確認済み |
| 送信失敗時のアラーム | STG で `MAIL_DUPLICATE_SLACK_FAILED` → メトリクスフィルタ → `ALARM` まで確認済み。本番では未実施 |
| `Errors` アラーム | STG で確認済み。本番では未実施 |
| 本番でのアラーム実配信 | SNS 購読は確認済み。実際に発火したことはまだない |

## 未実装・次段階の候補

| 項目 | 理由 |
| --- | --- |
| 通知用 SQS と通知 Lambda の分離 | Slack 障害で検知処理を巻き込まないため |
| 通知のリトライと通知 DLQ | Slack の一時障害／継続障害を再処理できるようにするため |
| DynamoDB 書き込み失敗用の検知 DLQ | 検知履歴そのものを失わず、元イベントから再処理するため |
| イベント冪等化 | S3 重複配送で同じログ行が別レコードとして保存され、そのオブジェクト内の全 `EmailID` が偽の重複として通知されるため。ソートキーを `{sourceKey}#{行番号}` へ変えることで対応できる |
| 通知の抑制・冪等化 | 同時実行時に通知が2回飛ぶことがあるため（ローカル計測で5回中1回） |
| アラーム通知先の Slack 化（AWS Chatbot） | 重複検知は Slack、アラームはメール、と分かれると運用時に見落としやすいため。本番では SNS + メール購読で先に経路を成立させ、Chatbot は後続で検討する |
| Lambda の実行時間短縮 | 1行あたり2回のネットワーク往復が処理時間を決めている。`BatchWriteItem` で書き込みをまとめれば往復回数を約1/25にできる。現状はメモリ1024MBとタイムアウト300秒で必要な流量を満たしているため未着手 |
| Fluent Bit のフラッシュ間隔短縮 | 1オブジェクトあたりの行数を抑える最も確実な手段。1分間隔ならピークでも240行に収まる |
| 対象外オブジェクトのログ抑制 | S3 通知にフィルタを掛けられないため、無関係なオブジェクトでも起動し `Received S3 event` がイベント全文とともに出力される。プレフィックス判定を通過してから出力すれば、対象外は1行に収まる |
| ログ再送への耐性 | **Fluent Bit によるログ再送が実在することを 2026-09-17 の調査で確認**（9/10 のログ4,666行が9/15に再送）。現在は `logTimestamp` に Fluent Bit の取り込み時刻を優先して入れているため、再送されると別の行として扱われる。Laravel の出力時刻を優先し、件数を `logTimestamp` の種類数で数えるようにすれば、再送が何度起きても1件になる。今回の再送は5日遅れで判定窓の外だったが、1時間以内に起きると誤検知する |
| S3 通知フィルタの原因特定 | URL エンコード形式（`service%3Dworker/...`）での検証が未実施。解決すれば STG を再開できる |
| Lambda 死活監視 | `Errors` アラームは「起動して失敗した」しか拾えない。S3 通知の破損や Fluent Bit 停止で「そもそも起動しない」場合は検出できず、`treat-missing-data notBreaching` により `OK` のままになる。`Invocations` が一定時間ゼロであることを異常とする監視（`LessThanThreshold` + `treat-missing-data breaching`）が必要。**2026-09-16 時点では未設定。** 導入初日の3時間の実測は 160 / 163 / 164 回/時（±2%）と安定していたが、**夜間のデータが無い**。メンテナンス窓や EB のデプロイで起動ゼロの時間帯があると初日から誤報になり、アラームの信頼を損なうため、24時間ぶんの最小値を確認してからしきい値を決める。<br><br>なお起動の約92%は対象外オブジェクト（メールログは12件/時、全体の約7.5%）であるため、この監視は「バケット全体が生きているか」を見るものであり、**メールログだけが届かなくなった状態は検出できない**。それを見るには `Matched successful mail send` のメトリクスフィルタが要るが、夜間は送信ゼロの時間帯があるため窓を長く取る設計が必要 |

DLQ は単なる保管場所である。再実行にはリドライブまたは再処理 Lambda を別途設計する。

## 段階導入の目安

1. ~~現在の構成を STG へ小さく導入し、実ログ量と通知内容を確認する。~~ **2026-09-15 完了**（上記「STG環境での検証結果」を参照）
2. ~~Slack Incoming Webhook を設定し、通知本文・送信失敗経路・CloudWatch アラームを検証する。~~ **2026-09-16 完了**（上記「通知の内容と処理性能」を参照）
3. ~~本番環境へ導入する。~~ **2026-09-16 完了**（上記「本番環境への導入」を参照）
4. 本番で最初の重複が検知されるまで、`Matched successful mail send` の件数と Duration を定期的に確認する。1オブジェクトあたりの行数が想定（ピーク2,400行）に対してどうか、実流量で見る。
5. S3 イベント通知のフィルタが効かない原因を特定する。解決すれば STG を再開でき、無関係なオブジェクトでの起動も無くなる。
6. Slack 障害への対応が必要になった時点で、通知を SQS + 通知 Lambda + 通知 DLQ へ分離する。
7. DynamoDB 失敗や再実行の要件が明確になった時点で、検知 DLQ と冪等化を追加する。
