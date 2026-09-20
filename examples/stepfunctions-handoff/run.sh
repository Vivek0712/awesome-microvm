#!/usr/bin/env bash
# Start one lease execution, show the VM it launches, poll until the execution ends, print its output.
#
#   ./run.sh                                  # reads the state machine ARN from the stack
#   ARN=arn:aws:states:...:stateMachine:x ./run.sh
#   STACK_NAME=my-stack AWS_PROFILE=me AWS_REGION=us-east-1 ./run.sh
set -euo pipefail

STACK_NAME="${STACK_NAME:-microvm-sfn-handoff}"
IMAGE="${IMAGE:-handoff-agent}"
TASK='{"steps":["echo hello","python3 -c \"print(2+2)\"","sleep 5"]}'
AWS_ARGS=()
[ -n "${AWS_PROFILE:-}" ] && AWS_ARGS+=(--profile "$AWS_PROFILE")
[ -n "${AWS_REGION:-}" ] && AWS_ARGS+=(--region "$AWS_REGION")

if [ -z "${ARN:-}" ]; then
  ARN=$(aws "${AWS_ARGS[@]}" cloudformation describe-stacks --stack-name "$STACK_NAME" \
        --query "Stacks[0].Outputs[?OutputKey=='StateMachineArn'].OutputValue" --output text)
fi
[ -n "$ARN" ] || { echo "no state machine ARN: deploy the stack or set ARN" >&2; exit 1; }

NAME="lease-$(date +%s)"
echo "$ aws stepfunctions start-execution --name $NAME --input '$TASK'"
EXEC=$(aws "${AWS_ARGS[@]}" stepfunctions start-execution --state-machine-arn "$ARN" --name "$NAME" \
       --input "$TASK" --query executionArn --output text)
echo "execution $EXEC"

STATUS=RUNNING
TICK=0
while [ "$STATUS" = RUNNING ]; do
  sleep 3
  STATUS=$(aws "${AWS_ARGS[@]}" stepfunctions describe-execution --execution-arn "$EXEC" --query status --output text)
  echo "$(date +%H:%M:%S)  $STATUS"
  TICK=$((TICK + 1))
  if [ "$TICK" -eq 2 ] && command -v mvm >/dev/null; then
    echo "$ mvm ls --image $IMAGE"
    mvm ls --image "$IMAGE" || true
  fi
done

echo "--- output ---"
aws "${AWS_ARGS[@]}" stepfunctions describe-execution --execution-arn "$EXEC" --query '{status: status, output: output, error: error, cause: cause}' --output json
