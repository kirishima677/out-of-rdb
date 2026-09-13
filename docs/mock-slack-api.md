# LocalStack 疑似 Slack API

Slack の Incoming Webhook の代わりに、LocalStack の API Gateway と Lambda で動くテスト API です。呼び出し回数は DynamoDB Local の `mock_slack_api_calls` テーブルへ保存されます。

## 起動

```bash
docker compose up -d --force-recreate localstack
```

URL に含まれる API ID を取得します。

```bash
API_ID="$(docker exec localstack awslocal apigateway get-rest-apis \
  --query 'items[?name==`mock-slack-api`].id | [0]' --output text)"
API_URL="http://localhost:4566/restapis/${API_ID}/local/_user_request_/slack"
```

## シナリオ

すべて JSON の POST を受け付けます。`-H` と `-d` は Slack Webhook 呼び出しに近い形を再現するためのものです。

```bash
# 常に HTTP 200
curl -i -X POST "${API_URL}/always-success" \
  -H 'Content-Type: application/json' -d '{"text":"test"}'

# 常に HTTP 500
curl -i -X POST "${API_URL}/always-failure" \
  -H 'Content-Type: application/json' -d '{"text":"test"}'

# 1、2回目は HTTP 500、3回目以降は HTTP 200
curl -i -X POST "${API_URL}/fail-twice-then-success?reset=true"
curl -i -X POST "${API_URL}/fail-twice-then-success" -d '{"text":"test"}'
curl -i -X POST "${API_URL}/fail-twice-then-success" -d '{"text":"test"}'
curl -i -X POST "${API_URL}/fail-twice-then-success" -d '{"text":"test"}'
```

`reset=true` は試行回数を 0 に戻すだけで、配送試行には数えません。

## 注意

これは Slack 連携のリトライ動作を検証するための疑似 API です。ローカル起動時、メール重複検知 Lambda は `always-success` URL を `SLACK_WEBHOOK_URL` として自動設定されます。`test-mail-duplicate-detector.sh` は検知ログに加え、この API がちょうど1回呼び出されたことも確認します。
