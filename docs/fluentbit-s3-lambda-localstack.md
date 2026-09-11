# Fluent Bit → S3 → Lambda → DynamoDB Local サンプル

このドキュメントでは、このリポジトリに追加したローカルのイベント処理環境を説明します。

Fluent Bit が出力したログを LocalStack の S3 互換 API へ配送し、S3 のオブジェクト作成イベントで LocalStack の Lambda を起動します。Lambda は対象オブジェクトを読み取り、処理結果を既存の DynamoDB Local に保存します。

実AWSのアカウント、アクセスキー、S3バケットは不要です。すべて Docker Compose のネットワーク内で動作します。

## 全体像

```text
┌─────────────┐      PutObject       ┌──────────────────────┐
│ Fluent Bit  │ ───────────────────▶ │ LocalStack S3         │
│ dummy input │                      │ fluentbit-logs bucket │
└─────────────┘                      └──────────┬───────────┘
                                                  │ ObjectCreated:Put
                                                  ▼
                                      ┌──────────────────────┐
                                      │ LocalStack Lambda     │
                                      │ process-fluentbit-log │
                                      └──────────┬───────────┘
                                                  │ PutItem
                                                  ▼
                                      ┌──────────────────────┐
                                      │ DynamoDB Local       │
                                      │ processed_logs table │
                                      └──────────────────────┘
```

各コンテナは Docker Compose の `labnet` に接続されています。Lambda は LocalStack が Docker ソケット経由で実行用コンテナを一時的に作成して実行します。そのため、Lambda 実行用コンテナも `labnet` に参加できるよう設定されています。

## 構成要素

| 要素 | 役割 | 主な設定／ファイル |
| --- | --- | --- |
| LocalStack | S3、Lambda、IAM、CloudWatch Logs、STS のローカルエミュレーション | `docker-compose.yaml` の `localstack` |
| Fluent Bit | サンプルログを S3 へ配送 | `fluent-bit/fluent-bit.conf` |
| 初期化スクリプト | S3バケット、Lambda、S3イベント通知を作成 | `localstack/init/ready.d/01-bootstrap-fluentbit-sample.sh` |
| Lambda | S3オブジェクトを読み、処理結果を DynamoDB に保存 | `localstack/init/ready.d/process_fluentbit_log.py` |
| DynamoDB Local | Lambda の処理結果を保存 | Compose の `dynamodb` |

LocalStack は Community 版の `localstack/localstack:3.8.1` を使用しています。`latest` はライセンス認証が必要になる場合があるため、この学習用環境では固定しています。

## 起動

リポジトリのルートで実行します。

```bash
docker compose up -d
docker compose ps
```

初回はコンテナイメージの取得に時間がかかります。`localstack` が `healthy` になった後、`fluentbit` が開始されます。

状態の確認:

```bash
docker compose ps localstack fluentbit dynamodb
docker exec localstack awslocal s3 ls
```

`awslocal` は LocalStack コンテナ内に入っている AWS CLI ラッパーです。`http://localhost:4566` を LocalStack のエンドポイントとして自動設定します。

## サンプルの動作

### 1. LocalStack の初期化

LocalStack が利用可能になると、`localstack/init/ready.d/01-bootstrap-fluentbit-sample.sh` が実行されます。このスクリプトは以下を行います。

1. `fluentbit-logs` バケットを作成する
2. Lambda の Python コードを ZIP 化する
3. `process-fluentbit-log` 関数を作成または更新する
4. Lambda が `Active` になるまで待機する
5. S3 の `s3:ObjectCreated:*` 通知を Lambda に関連付ける

通知設定を確認するには、次を実行します。

```bash
docker exec localstack awslocal s3api get-bucket-notification-configuration \
  --bucket fluentbit-logs
```

### 2. Fluent Bit によるログ配送

`fluentbit` は `dummy` 入力プラグインで次の1件だけを生成します。

```json
{
  "message": "hello from Fluent Bit",
  "service": "sample"
}
```

S3 出力プラグインはログを JSON Lines としてオブジェクトに保存します。ローカルで素早くイベントを発生させるため、次の設定を使用しています。

| 設定 | 値 | 意味 |
| --- | --- | --- |
| `Endpoint` | `http://localstack:4566` | 実AWSではなく LocalStack の S3 API を利用する |
| `Use_Put_Object` | `On` | マルチパートではなく `PutObject` でアップロードする |
| `Upload_Timeout` | `5s` | 少量のログでも最大5秒程度でオブジェクトを確定する |
| `S3_Key_Format` | `/logs/.../$UUID.json` | 重複しないS3キーを生成する |
| `Store_Dir` | `/buffers` | S3 配送用のバッファ保存先 |

配送ログを確認します。

```bash
docker compose logs --tail=100 fluentbit
```

成功時には次のような出力が表示されます。

```text
Successfully uploaded object /logs/2026/09/11/13/27/07-xxxxxxxx.json
```

### 3. S3イベントと Lambda

S3 にオブジェクトが作成されると、`ObjectCreated:Put` イベントが Lambda に渡されます。Lambda はイベントの `Records` からバケット名とオブジェクトキーを取得し、S3 API でオブジェクト本文を取得します。

Lambda には次の環境変数を設定しています。

| 変数 | 値 | 用途 |
| --- | --- | --- |
| `LOCALSTACK_ENDPOINT` | `http://localstack:4566` | Lambda から LocalStack S3 を読む接続先 |
| `DYNAMODB_ENDPOINT` | `http://dynamodb:8000` | Lambda から既存 DynamoDB Local へ書く接続先 |

受け取った S3イベントは CloudWatch Logs 互換のロググループへ出力されます。

```bash
docker exec localstack awslocal logs filter-log-events \
  --log-group-name /aws/lambda/process-fluentbit-log
```

`Received S3 event` と `Processed s3://...` が表示されれば、イベント配送と処理が成功しています。

### 4. DynamoDB Local への保存

Lambda は `processed_logs` テーブルを必要に応じて作成します。その後、各S3オブジェクトについて次の項目を保存します。

| 属性 | 内容 |
| --- | --- |
| `object_key` | S3オブジェクトキー（パーティションキー） |
| `bucket` | バケット名 |
| `line_count` | 空行を除いたログ行数 |
| `content` | S3オブジェクトの本文 |

処理結果を確認します。

```bash
docker exec localstack awslocal dynamodb scan \
  --endpoint-url http://dynamodb:8000 \
  --table-name processed_logs
```

結果の例:

```json
{
  "bucket": { "S": "fluentbit-logs" },
  "line_count": { "N": "1" },
  "object_key": { "S": "logs/2026/09/11/...json" },
  "content": { "S": "{\"message\":\"hello from Fluent Bit\"}\n" }
}
```

## 再実行方法

Fluent Bit は起動ごとにサンプルのダミーログを1件送ります。再度イベントを発生させるには、Fluent Bit コンテナを再作成します。

```bash
docker compose up -d --force-recreate fluentbit
```

その後、S3オブジェクトと DynamoDB の保存結果を再度確認してください。S3キーに UUID を含むため、実行ごとに別オブジェクトとして保存されます。

## 実運用の Fluent Bit 設定へ置き換える場合

このサンプルの `dummy` 入力を、実際のログ入力へ置き換えます。たとえば Docker コンテナログを読むなら `tail` 入力、標準入力なら `stdin` 入力を使用します。S3出力の `Endpoint` は、実AWSへ配送する設定では削除します。

ローカル検証中は以下を維持します。

```ini
Endpoint        http://localstack:4566
Use_Put_Object  On
Upload_Timeout  5s
```

本番に近いファイルサイズや配送間隔を試したい場合は、`Total_File_Size` と `Upload_Timeout` を実際の Fluent Bit 設定と合わせます。S3オブジェクトが確定するまで Lambda は起動しないため、これらの値はイベント発生タイミングに直接影響します。

## よくある確認ポイント

### Fluent Bit が S3 に配送しない

```bash
docker compose logs --tail=200 fluentbit
docker exec localstack awslocal s3 ls s3://fluentbit-logs --recursive
```

`Endpoint` が `http://localstack:4566` になっていること、`localstack` が `healthy` であることを確認します。

### S3にはあるが Lambda が起動しない

```bash
docker exec localstack awslocal s3api get-bucket-notification-configuration \
  --bucket fluentbit-logs
docker exec localstack awslocal lambda get-function \
  --function-name process-fluentbit-log
docker compose logs --tail=200 localstack
```

通知設定に `process-fluentbit-logs` があり、Lambda の状態が `Active` であることを確認します。

### Lambda は起動するが DynamoDB に保存されない

```bash
docker exec localstack awslocal logs filter-log-events \
  --log-group-name /aws/lambda/process-fluentbit-log
docker compose logs --tail=200 dynamodb
```

Lambda から DynamoDB Local へは `http://dynamodb:8000` で接続します。LocalStack の DynamoDB ではなく、既存の `dynamodb` コンテナを参照している点に注意してください。

## データのリセット

通常の停止・起動では named volume が残るため、S3オブジェクトや DynamoDB のデータは保持されます。

```bash
docker compose down
docker compose up -d
```

すべてのローカルデータを削除して最初から試す場合は、次のコマンドを使います。この操作は MongoDB、Redis、DynamoDB Local など、この Compose プロジェクトの全ボリュームを削除するため注意してください。

```bash
docker compose down -v
docker compose up -d
```

## 実AWSとの差分と使い分け

LocalStack はローカルでの開発速度を上げるための AWS API エミュレータです。S3→Lambda→DynamoDB の基本的なイベント連携を素早く確認する用途に適しています。

一方、IAM ポリシーの細かな評価、すべてのサービス機能、性能、同時実行、失敗時の再試行や順序保証は実AWSと完全には一致しません。ローカルでは処理ロジックと連携フローを反復確認し、リリース前には実AWSの検証環境でも統合テストを行ってください。
