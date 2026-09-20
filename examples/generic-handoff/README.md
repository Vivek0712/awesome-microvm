# Generic handoff: any controller

No Step Functions, no durable function: a plain Python script on a laptop leases a [handoff-agent](../handoff-agent) VM with `FleetManager.lease(...)`, then waits for the VM to report back over the transport of your choice. All three transports end in one SQS queue the controller long-polls, so the loop (launch, print heartbeats as they land, print the typed result or failure, terminate, enforce the budget) is identical; only the setup differs.

| `--kind` | What the controller creates | What the VM does | VM role needs |
|---|---|---|---|
| `sqs` | standard queue `microvm-lease-sqs` | `sqs:SendMessage` of heartbeat/success/failure bodies to the queue | `sqs:SendMessage` on the queue |
| `eventbridge` | bus `microvm-lease`, rule `source = microvm.lease` targeting queue `microvm-lease-eventbridge` | `events:PutEvents` with detail types `microvm.lease.{heartbeat,success,failure}` | `events:PutEvents` on the bus |
| `http` | nothing: expects the [collector](collector/) stack (a Function URL that appends each POST to a queue) | `POST` JSON with `Authorization: Bearer <token>` to the collector | nothing |

```console
pip install microvm-ctl rich
mvm bootstrap && mvm image build handoff-agent examples/handoff-agent

python3 controller.py --kind sqs --runs 3
python3 controller.py --kind eventbridge --task '{"steps":["echo hi"],"fail_after_s":5}'   # typed retryable failure
python3 controller.py --kind sqs --task '{"steps":[],"hang_s":900}' --budget 60              # client-side timed_out, VM terminated

sam deploy -t collector/template.yaml --stack-name microvm-lease-collector --resolve-s3 --capabilities CAPABILITY_IAM
python3 controller.py --kind http                                                            # target and queue read from the stack outputs
python3 controller.py --kind http --target https://<id>.lambda-url.<region>.on.aws/ --queue-url https://sqs...
```

The controller prints the IAM statement the VM's execution role needs on the first line (`mvm lease policy --kind sqs --orchestrator <queue arn>` prints the same JSON); attach it to `MVM_EXECUTION_ROLE_ARN` once. Every message is matched on the per-run token, so a late heartbeat from an earlier run is discarded, not misattributed. The budget is enforced client-side (the VM's `maximumDurationInSeconds` is budget plus slack, so a hung VM also dies on its own), a `timed_out` run is terminated and reported like any other, and the summary table gives launch-to-first-message, VM work time, and total per run plus p50s across `--runs`, also written to `results-<kind>-<timestamp>.json`.

The collector does not verify the bearer token; it forwards it, and the controller decides. Put a real check (a secret compared in the handler, or `AuthType: AWS_IAM` with SigV4 from the VM) in front of it before pointing anything untrusted at the URL.
