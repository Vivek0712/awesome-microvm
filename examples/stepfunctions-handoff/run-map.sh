#!/usr/bin/env bash
# Start one fan-out execution of the Map machine with N shards, poll until it ends, print its output.
#
#   ./run-map.sh --shards 8                          # reads the state machine ARN from the map stack
#   ARN=arn:aws:states:...:stateMachine:x ./run-map.sh --shards 4
#   STACK_NAME=my-map-stack AWS_PROFILE=me AWS_REGION=us-east-1 ./run-map.sh --shards 4
set -euo pipefail

SHARDS=4
while [ $# -gt 0 ]; do
  case "$1" in
    --shards) SHARDS="${2:-}"; shift 2 ;;
    --shards=*) SHARDS="${1#*=}"; shift ;;
    *) echo "usage: $0 [--shards N]" >&2; exit 2 ;;
  esac
done
case "$SHARDS" in
  ''|*[!0-9]*|0) echo "--shards must be a positive integer, got '$SHARDS'" >&2; exit 2 ;;
esac

STACK_NAME="${STACK_NAME:-microvm-sfn-handoff-map}"
IMAGE="${IMAGE:-handoff-agent}"
TASK='{"steps":["echo shard","sleep 3"]}'
AWS_ARGS=()
[ -n "${AWS_PROFILE:-}" ] && AWS_ARGS+=(--profile "$AWS_PROFILE")
[ -n "${AWS_REGION:-}" ] && AWS_ARGS+=(--region "$AWS_REGION")

if [ -z "${ARN:-}" ]; then
  ARN=$(aws "${AWS_ARGS[@]}" cloudformation describe-stacks --stack-name "$STACK_NAME" \
        --query "Stacks[0].Outputs[?OutputKey=='StateMachineArn'].OutputValue" --output text)
fi
[ -n "$ARN" ] || { echo "no state machine ARN: deploy template-map.yaml or set ARN" >&2; exit 1; }

# the input is {"shards": [task, task, ...]}: one lease, one VM, one token per element
INPUT=$(python3 -c 'import json, sys; print(json.dumps({"shards": [json.loads(sys.argv[2])] * int(sys.argv[1])}))' \
        "$SHARDS" "$TASK")

NAME="fanout-$SHARDS-$(date +%s)"
echo "$ aws stepfunctions start-execution --name $NAME --input '{\"shards\": [$TASK x $SHARDS]}'"
EXEC=$(aws "${AWS_ARGS[@]}" stepfunctions start-execution --state-machine-arn "$ARN" --name "$NAME" \
       --input "$INPUT" --query executionArn --output text)
echo "execution $EXEC"

STATUS=RUNNING
TICK=0
while [ "$STATUS" = RUNNING ]; do
  sleep 3
  STATUS=$(aws "${AWS_ARGS[@]}" stepfunctions describe-execution --execution-arn "$EXEC" --query status --output text)
  echo "$(date +%H:%M:%S)  $STATUS"
  TICK=$((TICK + 1))
  if [ "$TICK" -eq 2 ] && command -v mvm >/dev/null; then
    echo "$ mvm ls --image $IMAGE            # one row per shard; \`mvm watch --image $IMAGE\` follows them live"
    mvm ls --image "$IMAGE" || true
  fi
done

echo "--- output (an array with one VM payload per shard) ---"
aws "${AWS_ARGS[@]}" stepfunctions describe-execution --execution-arn "$EXEC" --query '{status: status, output: output, error: error, cause: cause}' --output json
