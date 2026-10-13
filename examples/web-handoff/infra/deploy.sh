#!/usr/bin/env bash
# Deploy the Web Function, its jobs table and execution role, then put limits on its endpoint.
#
#   DEMO_KEY=... ./infra/deploy.sh
#
# Needs AWS CLI 2.37 or later (aws lambda-web) and Node 22 or later to build.
set -euo pipefail
cd "$(dirname "$0")/.."
NAME=${NAME:-microvm-web-handoff}
REGION=${AWS_REGION:-us-east-1}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
IMAGE=${MICROVM_IMAGE_ARN:-arn:aws:lambda:$REGION:$ACCOUNT:microvm-image:handoff-agent-small}
TABLE=$NAME-jobs
ROLE=$NAME-fn
: "${DEMO_KEY:?set DEMO_KEY, the key callers send in x-demo-key}"

if ! aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" >/dev/null 2>&1; then
  aws dynamodb create-table --table-name "$TABLE" --region "$REGION" --billing-mode PAY_PER_REQUEST \
    --attribute-definitions AttributeName=id,AttributeType=S --key-schema AttributeName=id,KeyType=HASH >/dev/null
  aws dynamodb wait table-exists --table-name "$TABLE" --region "$REGION"
  aws dynamodb update-time-to-live --table-name "$TABLE" --region "$REGION" \
    --time-to-live-specification Enabled=true,AttributeName=expires >/dev/null
fi

if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document file://infra/trust.json >/dev/null
  sleep 10
fi
sed -e "s/{region}/$REGION/g; s/{account}/$ACCOUNT/g; s/{name}/$NAME/g; s/{table}/$TABLE/g" infra/policy.json > /tmp/$NAME-policy.json
aws iam put-role-policy --role-name "$ROLE" --policy-name run-microvm-jobs --policy-document file:///tmp/$NAME-policy.json

npm ci --silent && npm run build --silent
aws lambda-web deploy --name "$NAME" --code dist --create --region "$REGION" \
  --execution-role-arn "arn:aws:iam::$ACCOUNT:role/$ROLE" \
  --timeout-seconds 900 --max-concurrency-per-environment 64 \
  --env "MICROVM_IMAGE_ARN=$IMAGE" --env "JOBS_TABLE=$TABLE" --env "DEMO_KEY=$DEMO_KEY" \
  --env "JOB_BUDGET_S=${JOB_BUDGET_S:-300}" --env "LAUNCHES_PER_HOUR=${LAUNCHES_PER_HOUR:-30}"

aws lambda-web get-web-function-endpoint --function-name "$NAME" --endpoint-name dev --region "$REGION" \
  --query '{domain: domainName, state: state}' --output table

# guardrails: at most 2 environments and 100 requests a second on the endpoint (the lowest non-zero rate limit), on top of the key and
# the hourly launch budget the function enforces itself
aws lambda-web update-web-function-endpoint --function-name "$NAME" --endpoint-name dev --region "$REGION" \
  --scaling-config maxEnvironments=${MAX_ENVIRONMENTS:-2} --throttle-config rateLimit=${RATE_LIMIT:-100} \
  --query '{scaling: scalingConfig, throttle: throttleConfig}' --output json
