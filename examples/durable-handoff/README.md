# Durable function to MicroVM handoff

A Lambda durable function *leases* a Lambda MicroVM to one task and suspends until the VM calls back. The callback id is the lease: it goes out in `runHookPayload` at launch, the VM heartbeats it while it works, completes it with a typed result or a typed failure, and the orchestrator terminates the VM in every branch it can see. Built on [microvm-ctl](https://github.com/Vivek0712/microvm-ctl); shaped after module 2 of the AWS workshop *Running AI agent-generated code securely*, with the gaps closed.

```
GitHub / CodeCommit ──▶ webhook.py ──▶ orchestrator (durable) ──create_callback──▶ RunMicrovm(runHookPayload={callback_id, task})
                                             │                                             │
                                             │ callback.result()  ◀── heartbeat every 30 s ┤  clone, diff, bandit, secret scan,
                                             │ (suspended, $0)    ◀── Success / Failure ───┘  post review on the PR
                                             └──▶ TerminateMicrovm         janitor every 5 min · maximumDurationInSeconds = budget + slack
```

## What is in here

| Path | Runs where | Role |
|---|---|---|
| `agent/` | inside the MicroVM | security review agent on the zero-dependency hook runtime: `/run` takes the lease, a thread does the work, heartbeats and completes the callback |
| `orchestrator/app.py` | Lambda durable function, Python 3.13 | `microvm.integrations.durable.lease_with_relaunch`: `create_callback` → `FleetManager.lease` (at-most-once step, `clientToken` from the callback id) → `callback.result()` → terminate, relaunched once on a retryable outcome; this file adds event parsing, the CodeCommit PR lookup, and the `lease_map` fan-out |
| `orchestrator/webhook.py` | Lambda behind a Function URL | verifies the GitHub signature, starts one execution per PR head SHA (`DurableExecutionName`), so redeliveries reattach |
| `orchestrator/janitor.py` | Lambda on a 5 minute schedule | `Fleet.reap()` for VMs older than budget + slack: covers operator stops that skip the orchestrator's cleanup |
| `orchestrator/template.yaml` | SAM | the three functions, `DurableConfig`, the alias, and the execution role the agent VM runs as, with callback permissions scoped to this orchestrator |

## Run it

The stack in the article was last deployed from PyPI `microvm-ctl[durable]>=0.3.0` with nothing local; a single lease on `handoff-agent-small` then finished in 8.4 s and a four-shard `lease_map` in 17.5 s, both by the service's own start and stop times.

```console
pip install microvm-ctl
mvm bootstrap                                                            # once: bucket + build role
mvm image build security-review-agent examples/durable-handoff/agent \
    --env GITHUB_TOKEN_SECRET_ARN=arn:aws:secretsmanager:...:secret:gh-token   # omit for CodeCommit-only

cd examples/durable-handoff/orchestrator
sam build && sam deploy --parameter-overrides \
    ImageName=security-review-agent \
    GitHubTokenSecretArn=arn:aws:secretsmanager:...:secret:gh-token \
    WebhookSecretArn=arn:aws:secretsmanager:...:secret:gh-webhook
```

`ImageName` can be any image whose app uses `@app.on_lease`: the security review agent here, or `handoff-agent` from `examples/handoff-agent` if you want to hand the durable function plain shell steps (`{"task": {"steps": [...]}}`), which is how the benchmark drives it.

Fan-out: invoke with `{"mode": "fanout", "shards": [task, task, ...]}` and the orchestrator calls `microvm.integrations.durable.lease_map`. A `shard-plan` step sizes the fan-out from the account's memory quota and the VM baseline (`BASELINE_MIB`, default 2048) and returns `{"status": "rejected", "reason": ...}` before anything launches when it cannot fit; otherwise `context.map` leases one VM per shard, at most the plan's concurrency (or `MAX_CONCURRENCY` when that is lower) at a time, each relaunched once on a retryable outcome, and the execution ends with `{"status": "done", "plan", "outcomes", "succeeded", "failed"}`, the plan's one-sentence summary (`8 shards on 2 GB: 4 at a time (memory quota 8 GB / 2 GB baseline), 2 waves, ...`) in the step log. With a `task` in the event as well, each shard is merged into it (an object) or becomes its `paths` (a list), which is how one review is split by path; the benchmark's `--fanout 4,8 --fanout-kinds durable` sends whole tasks. `mvm watch --image <ImageName>` shows one row per shard VM while it runs.

The stack prints three outputs: `WebhookUrl` (GitHub webhook target, `pull_request` events, JSON), `OrchestratorAlias` (point a CodeCommit trigger at it, or invoke it yourself), and `AgentExecutionRoleArn` (use as `MVM_EXECUTION_ROLE_ARN` when you run the agent by hand).

Try one lease without any git hosting wired up:

```console
aws lambda invoke --function-name microvm-durable-handoff-orchestrator:live \
    --invocation-type Event --cli-binary-format raw-in-base64-out \
    --durable-execution-name demo-1 \
    --payload '{"task":{"provider":"github","repo":"psf/requests","pr":1,"base":"<sha>","head":"<sha>","post":false}}' /dev/stdout

mvm top --watch                                     # the leased VM appears, works, and goes
aws lambda list-durable-executions-by-function --function-name microvm-durable-handoff-orchestrator
aws lambda get-durable-execution-history --durable-execution-arn <arn>   # CallbackStarted ... CallbackSucceeded
```

Try the agent alone, no durable function:

```console
export MVM_EXECUTION_ROLE_ARN=<AgentExecutionRoleArn>
mvm run security-review-agent --wait
mvm call <id> /review -X POST -d '{"provider":"github","repo":"psf/requests","pr":1,"base":"<sha>","head":"<sha>","post":false}'
mvm call <id> /status
mvm terminate <id>
```

## Test the orchestrator without an AWS account

The durable SDK ships a local runner that drives the handler through checkpoints, callbacks, timeouts, and replays. `test_orchestrator.py` fakes only the fleet manager and covers the three paths a lease can take.

```console
cd examples/durable-handoff/orchestrator
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt aws-durable-execution-sdk-python-testing pytest
.venv/bin/pytest -q test_orchestrator.py          # success · retryable failure then fatal · silent agent times out · fan-out of two · rejected plan
```

## The three invariants

1. **One launch, once.** The launch is a step with at-most-once semantics and no SDK retries, and the `RunMicrovm` `clientToken` is derived from the callback id (`microvm.lease.client_token`). A replay after a crash between the API call and the checkpoint gets the same VM back.
2. **The lease rides in `runHookPayload`.** No polling for RUNNING, no token mint, no dispatch call: the VM starts working when `/run` fires. The payload is capped at 4,096 characters, so pass pointers, never bodies.
3. **Time is bounded twice.** Callback timeout = job budget; heartbeat timeout 120 s against a 30 s heartbeat; `maximumDurationInSeconds` = budget + 120 s on the VM; a janitor every five minutes. An operator's `StopDurableExecution` kills the invocation at the next checkpoint without running any `except`, so the last two are not optional.

The rule that is easy to get wrong: never wrap `callback.result()` in `try/finally`. The SDK suspends the execution by raising `SuspendExecution`, a `BaseException`, from `result()`, so a `finally` would terminate the VM at the moment the function goes to sleep (a bare `except Exception` does not catch it, but hides real errors). `microvm.integrations.durable.lease_microvm` catches `CallbackTimeoutError` and `CallbackExternalError`, terminates in each branch, and returns an outcome whose `retryable` flag decides whether `lease_with_relaunch` launches again.

## Failure matrix

| What breaks | Who notices | What happens to the VM | What the execution sees |
|---|---|---|---|
| clone fails | agent | `SendDurableExecutionCallbackFailure(CloneFailed, retryable)` | `CallbackExternalError`, terminate, relaunch once |
| agent process dies | orchestrator, via heartbeat timeout (≤ 120 s) | terminate | `CallbackTimeoutError`, outcome `timed_out` |
| review runs past the budget | orchestrator, via callback timeout | terminate; agent's next heartbeat gets `CallbackTimeoutException` and aborts | `CallbackTimeoutError` |
| orchestrator invocation crashes mid-launch | SDK replay | same VM via `clientToken` | step re-runs, no duplicate VM |
| operator stops the execution | nobody in code | `maximumDurationInSeconds`, then the janitor | `STOPPED` in history |
| duplicate webhook delivery | webhook receiver | none | reattaches to the running execution |
| VM terminated by the janitor mid-review | agent's `/terminate` hook | already gone | `Terminated` failure callback, relaunch once |

Deep dive: [The handoff lease](../../blog/deep-dives/09-durable-handoff.md). Reference lab: module 2 of the AWS workshop *Running AI agent-generated code securely* (Kiro reviewer orchestrated by a durable function over `wait_for_condition` polling and an endpoint dispatch); this example keeps its shape and replaces the polling, the dispatch hop, and the unbounded wait.
