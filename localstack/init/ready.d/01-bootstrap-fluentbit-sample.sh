#!/bin/sh
set -eu

BUCKET=fluentbit-logs
FUNCTION=process-fluentbit-log
ROLE_ARN=arn:aws:iam::000000000000:role/localstack-lambda-role
FUNCTION_ARN=arn:aws:lambda:us-east-1:000000000000:function:${FUNCTION}

awslocal s3 mb "s3://${BUCKET}" 2>/dev/null || true

python3 - <<'PY'
from zipfile import ZIP_DEFLATED, ZipFile

with ZipFile("/tmp/process-fluentbit-log.zip", "w", ZIP_DEFLATED) as archive:
    archive.write(
        "/etc/localstack/init/ready.d/process_fluentbit_log.py",
        "process_fluentbit_log.py",
    )
PY

if awslocal lambda get-function --function-name "${FUNCTION}" >/dev/null 2>&1; then
  awslocal lambda update-function-code \
    --function-name "${FUNCTION}" \
    --zip-file fileb:///tmp/process-fluentbit-log.zip >/dev/null
else
  awslocal lambda create-function \
    --function-name "${FUNCTION}" \
    --runtime python3.12 \
    --handler process_fluentbit_log.handler \
    --role "${ROLE_ARN}" \
    --timeout 30 \
    --environment '{"Variables":{"LOCALSTACK_ENDPOINT":"http://localstack:4566","DYNAMODB_ENDPOINT":"http://dynamodb:8000"}}' \
    --zip-file fileb:///tmp/process-fluentbit-log.zip >/dev/null
fi

# LocalStack creates Lambda functions asynchronously, like AWS does.
# LocalStack は AWS と同様に Lambda 関数を非同期で作成する。
awslocal lambda wait function-active-v2 --function-name "${FUNCTION}"

cat >/tmp/s3-notification.json <<EOF
{
  "LambdaFunctionConfigurations": [
    {
      "Id": "process-fluentbit-logs",
      "LambdaFunctionArn": "${FUNCTION_ARN}",
      "Events": ["s3:ObjectCreated:*"]
    }
  ]
}
EOF

awslocal s3api put-bucket-notification-configuration \
  --bucket "${BUCKET}" \
  --notification-configuration file:///tmp/s3-notification.json

echo "Fluent Bit sample ready: s3://${BUCKET} -> ${FUNCTION}"

MAIL_BUCKET=mail-send-logs
MAIL_FUNCTION=detect-mail-duplicates
MAIL_FUNCTION_ARN=arn:aws:lambda:us-east-1:000000000000:function:${MAIL_FUNCTION}

awslocal s3 mb "s3://${MAIL_BUCKET}" 2>/dev/null || true

python3 - <<'PY'
from zipfile import ZIP_DEFLATED, ZipFile

with ZipFile("/tmp/detect-mail-duplicates.zip", "w", ZIP_DEFLATED) as archive:
    archive.write(
        "/etc/localstack/init/ready.d/detect_mail_duplicates.py",
        "detect_mail_duplicates.py",
    )
PY

if awslocal lambda get-function --function-name "${MAIL_FUNCTION}" >/dev/null 2>&1; then
  awslocal lambda update-function-code \
    --function-name "${MAIL_FUNCTION}" \
    --zip-file fileb:///tmp/detect-mail-duplicates.zip >/dev/null
else
  awslocal lambda create-function \
    --function-name "${MAIL_FUNCTION}" \
    --runtime python3.12 \
    --handler detect_mail_duplicates.handler \
    --role "${ROLE_ARN}" \
    --timeout 30 \
    --environment '{"Variables":{"LOCALSTACK_ENDPOINT":"http://localstack:4566","DYNAMODB_ENDPOINT":"http://dynamodb:8000"}}' \
    --zip-file fileb:///tmp/detect-mail-duplicates.zip >/dev/null
fi

awslocal lambda wait function-active-v2 --function-name "${MAIL_FUNCTION}"

cat >/tmp/mail-s3-notification.json <<EOF
{
  "LambdaFunctionConfigurations": [
    {
      "Id": "detect-mail-duplicates",
      "LambdaFunctionArn": "${MAIL_FUNCTION_ARN}",
      "Events": ["s3:ObjectCreated:*"]
    }
  ]
}
EOF

awslocal s3api put-bucket-notification-configuration \
  --bucket "${MAIL_BUCKET}" \
  --notification-configuration file:///tmp/mail-s3-notification.json

echo "Mail duplicate detector ready: s3://${MAIL_BUCKET} -> ${MAIL_FUNCTION}"

MOCK_SLACK_FUNCTION=mock-slack-api
MOCK_SLACK_API_NAME=mock-slack-api

python3 - <<'PY'
from zipfile import ZIP_DEFLATED, ZipFile

with ZipFile("/tmp/mock-slack-api.zip", "w", ZIP_DEFLATED) as archive:
    archive.write(
        "/etc/localstack/init/ready.d/mock_slack_api.py",
        "mock_slack_api.py",
    )
PY

awslocal lambda create-function \
  --function-name "${MOCK_SLACK_FUNCTION}" \
  --runtime python3.12 \
  --handler mock_slack_api.handler \
  --role "${ROLE_ARN}" \
  --timeout 30 \
  --environment '{"Variables":{"DYNAMODB_ENDPOINT":"http://dynamodb:8000"}}' \
  --zip-file fileb:///tmp/mock-slack-api.zip >/dev/null

awslocal lambda wait function-active-v2 --function-name "${MOCK_SLACK_FUNCTION}"
MOCK_SLACK_FUNCTION_ARN="$(awslocal lambda get-function --function-name "${MOCK_SLACK_FUNCTION}" --query 'Configuration.FunctionArn' --output text)"
MOCK_SLACK_API_ID="$(awslocal apigateway create-rest-api --name "${MOCK_SLACK_API_NAME}" --query id --output text)"
MOCK_SLACK_ROOT_ID="$(awslocal apigateway get-resources --rest-api-id "${MOCK_SLACK_API_ID}" --query 'items[?path==`/`].id | [0]' --output text)"
MOCK_SLACK_RESOURCE_ID="$(awslocal apigateway create-resource --rest-api-id "${MOCK_SLACK_API_ID}" --parent-id "${MOCK_SLACK_ROOT_ID}" --path-part slack --query id --output text)"
MOCK_SLACK_MODE_RESOURCE_ID="$(awslocal apigateway create-resource --rest-api-id "${MOCK_SLACK_API_ID}" --parent-id "${MOCK_SLACK_RESOURCE_ID}" --path-part '{mode}' --query id --output text)"

awslocal apigateway put-method \
  --rest-api-id "${MOCK_SLACK_API_ID}" \
  --resource-id "${MOCK_SLACK_MODE_RESOURCE_ID}" \
  --http-method POST \
  --authorization-type NONE >/dev/null
awslocal apigateway put-integration \
  --rest-api-id "${MOCK_SLACK_API_ID}" \
  --resource-id "${MOCK_SLACK_MODE_RESOURCE_ID}" \
  --http-method POST \
  --type AWS_PROXY \
  --integration-http-method POST \
  --uri "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/${MOCK_SLACK_FUNCTION_ARN}/invocations" >/dev/null
awslocal lambda add-permission \
  --function-name "${MOCK_SLACK_FUNCTION}" \
  --statement-id allow-apigateway \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:us-east-1:000000000000:${MOCK_SLACK_API_ID}/*/POST/slack/*" >/dev/null
awslocal apigateway create-deployment \
  --rest-api-id "${MOCK_SLACK_API_ID}" \
  --stage-name local >/dev/null

MOCK_SLACK_WEBHOOK_URL="http://localstack:4566/restapis/${MOCK_SLACK_API_ID}/local/_user_request_/slack/always-success"
awslocal lambda update-function-configuration \
  --function-name "${MAIL_FUNCTION}" \
  --environment "{\"Variables\":{\"LOCALSTACK_ENDPOINT\":\"http://localstack:4566\",\"DYNAMODB_ENDPOINT\":\"http://dynamodb:8000\",\"SLACK_WEBHOOK_URL\":\"${MOCK_SLACK_WEBHOOK_URL}\"}}" >/dev/null
awslocal lambda wait function-updated-v2 --function-name "${MAIL_FUNCTION}"

echo "Mock Slack API ready: http://localhost:4566/restapis/${MOCK_SLACK_API_ID}/local/_user_request_/slack/{always-success|always-failure|fail-twice-then-success}"
echo "Mail duplicate detector Slack target: ${MOCK_SLACK_WEBHOOK_URL}"
