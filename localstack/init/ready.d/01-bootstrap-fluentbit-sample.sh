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
