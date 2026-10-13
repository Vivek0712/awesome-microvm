Part 4 of this series handed a task to a Lambda MicroVM from a Lambda durable function. The callback id went into `runHookPayload`, the VM heartbeated it while it worked, and the VM completed it itself, so the function paid nothing while it waited. I built that contract into [microvm-ctl](https://github.com/Vivek0712/microvm-ctl) in Python. The AWS durable execution SDK for JavaScript now ships the same idea as part of the SDK: a `microvm` operation in `@aws/durable-execution-sdk-js-extras`, a `microvmSession` operation for several jobs on one VM, and `@aws/durable-execution-sdk-js-microvm-worker` for the code inside the VM. The packages are in the public [aws-durable-execution-sdk-js](https://github.com/aws/aws-durable-execution-sdk-js) repository, marked experimental, at version 0.1.0-alpha.0, and not yet on npm. I built them, ran every test they have against the live service, and then ran their end-to-end suite against a Python worker built on microvm-ctl to see whether the contract holds outside Node.

< FIGURE 1 of 1: upload blog/img/hook-05-durable-contract.png here >
Alt text: One job contract, two workers: a TypeScript durable function calls microvm(context, "build", input, config), which creates a callback, launches a MicroVM with a client token that is the SHA-256 of the callback id, delivers the job in the run hook or over HTTP when the input is over 4,096 code points, waits for the callback at no cost, and terminates the VM after success, failure and timeout alike; inside the VM either AWS's Node microvm-worker or microvm-ctl's Python HookApp.durable_job serves the job and sends heartbeats, success or failure back; both passed the SDK's 13 end-to-end scenarios, the Node worker 9 of 13 as shipped on an 8 GB account and 13 of 13 with capacity free, the Python worker 13 of 13; 382 of 382 unit tests and 2 of 2 examples passed.
*Measured live in us-east-1 on 2026-10-13, against aws-durable-execution-sdk-js at commit 571858c7.*

This is part 6 of Building on AWS Lambda MicroVMs. Every number below comes from my account in us-east-1 on 2026-10-13, which has the new-account defaults of 1 RunMicrovm call per second and 8 GB of MicroVM memory. The Python worker and its build script are in [awesome-microvm/examples/durable-sdk-worker](https://github.com/Vivek0712/awesome-microvm/tree/main/examples/durable-sdk-worker), and the runtime it uses shipped in microvm-ctl 0.5.0 on PyPI the same day.

## What the SDK gives you

`microvm` runs one job in a fresh MicroVM and returns the job's result to the durable function. The function's invocation ends while the VM works, so no Lambda compute is billed during the wait.

```typescript
import { withDurableExecution } from "@aws/durable-execution-sdk-js";
import { microvm } from "@aws/durable-execution-sdk-js-extras/microvm";

export const handler = withDurableExecution(async (event, context) =>
  microvm<{ passed: boolean }>(
    context,
    "build",
    { repo: event.repo, commit: event.commit },
    {
      imageIdentifier: process.env.MICROVM_IMAGE_ARN!,
      executionRoleArn: process.env.MICROVM_ROLE_ARN!,
      timeout: { hours: 1 },
      heartbeatTimeout: { seconds: 60 },
    },
  ),
);
```

Under that one call the operation runs five durable operations in a child context named after the job. `build.callback` creates a durable callback, so the callback id is checkpointed and stays the same on replay. `build.launch` calls RunMicrovm with a client token that is the SHA-256 of the callback id, so a replayed launch returns the same VM instead of a second one. microvm-ctl derives its client token from the lease token by the same rule, which I arrived at separately in part 4, and it was good to see the SDK land in the same place. If the job fits in the run hook's 4,096 code points it rides in the launch request, and the VM gets no ingress at all. If it does not fit, `build.request` mints an auth token and POSTs the job to the VM once it is up. The function then waits on the callback, and `build.terminate` ends the VM in every branch.

`microvmSession` keeps one VM for several jobs, so a later job can read what an earlier one wrote. The session handler sends jobs with `vm.invoke`, and it can wait on anything else the durable SDK offers in between, such as a human approval.

```typescript
const result = await microvmSession(context, "pipeline",
  { imageIdentifier, executionRoleArn, timeout: { hours: 2 }, autoSuspendOnIdle: true },
  async (vm, ctx) => {
    const build = await vm.invoke(ctx, "clone-build", { repo: event.repo },
      { path: "/clone-build", timeout: { minutes: 15 }, heartbeatTimeout: { seconds: 30 } });
    await ctx.waitForCallback("approval", sendApprovalRequest);
    return vm.invoke(ctx, "test", { build },
      { path: "/test", timeout: { minutes: 10 }, heartbeatTimeout: { seconds: 30 } });
  });
```

With `autoSuspendOnIdle`, the worker in the VM suspends its own VM when no job has run for a minute, and the session resumes it before the next job. A suspended VM pays only for its snapshot and keeps its memory and files, so a session that waits a day for an approval costs storage for that day.

Inside the VM, the Node worker takes a handler and optional named routes. It serves the lifecycle hooks, starts a heartbeat per job, reports the handler's value or error, and aborts `context.signal` when the durable function stops waiting or Lambda calls the terminate hook.

```typescript
import { startMicrovmWorker } from "@aws/durable-execution-sdk-js-microvm-worker";

await startMicrovmWorker({
  handler: async (input, context) => ({ passed: await runBuild(input.repo, { signal: context.signal }) }),
  routes: { "/clone-build": async (input, context) => cloneAndBuild(input, context.signal) },
});
```

## Running it as shipped

I built the four packages with Node 22.13 and ran both test suites. The extras package passed 145 tests in 12 suites and the worker passed 237 in 4, so 382 of 382.

The two published examples ran live. `microvm-run-job` counted 15 words, hashed the input, and returned the VM id in 7 s from start to result. `microvm-session` ran two jobs on one VM with a 5 s durable wait between them, and the second job read the file the first had written, in 15 s.

The end-to-end suite in `extras/e2e` is the most useful thing in the repository for anyone building on this. It deploys a durable function and a MicroVM image, then runs 13 scenarios that check the hard paths: a job that fails, a VM that crashes after its first heartbeat, a VM terminated mid-job, an HTTP job on a named route, inputs at the run hook limit in CJK and in emoji, a session that shares files, a session whose VM suspends itself between jobs, and two jobs in a `map`.

Run as shipped, all 13 at once, 9 passed. The other 4 failed after 2 seconds with `MicrovmLaunchError: The base maximum allocated memory limit has been reached for this account.` Thirteen VMs started together do not fit in 8 GB, and the launch step treats a quota error as permanent. I ran the same 4 again a minute later, after the first batch had released its memory, and all 4 passed in 11 to 16 s each. So 13 of 13 with capacity free.

Before any of that worked I lost some time to the AWS CLI. The runner shells out to `aws lambda-microvms` and `aws lambda invoke --durable-config`. AWS CLI 2.23 has neither, and the CLI v1 on my path had no `--cli-binary-format`, so every scenario failed with a usage message. AWS CLI 2.37.13 worked.

## The same suite against a Python worker

The worker's README specifies the contract precisely, down to which headers Lambda's own hook calls carry. A version 1 JSON payload with unknown fields ignored. The job in the run hook when it fits, a POST to `/durable-execution/v1/job` or a named route when it does not, answered 202 at once. One callback per job, heartbeats at a fraction of `heartbeatTimeoutSeconds`, the handler's value as the result with no envelope, and a failure with `ErrorType`, `ErrorMessage` and `ErrorData`. On the terminate hook, every running job fails with `MicrovmTerminatedError`. On the suspend hook, new jobs get 503 until resume.

None of that is specific to Node, so I added it to microvm-ctl's hook runtime as `HookApp.durable_job`. The runtime is one file on the Python standard library and boto3, and microvm-ctl injects it into every image it builds. A Python handler for the SDK's operations looks like this:

```python
from microvm_hooks import HookApp, LeaseError

app = HookApp()

@app.durable_job                      # a job with no route: in the run hook, or POST /durable-execution/v1/job
def build(input, ctx):
    ctx.job.phase("build")            # microvm-ctl job telemetry still works: GET /status, /events, mvm watch
    if ctx.cancelled.is_set():        # the durable function stopped waiting, or the VM is terminating
        raise LeaseError("Cancelled", "stopped")
    return {"passed": True}           # completes the durable callback with this value

@app.durable_job(path="/test")        # vm.invoke(ctx, "test", input, {path: "/test"}) in a session
def test(input, ctx):
    return {"ok": True}

app.serve(port=8080)
```

Then I wrote [app.py](https://github.com/Vivek0712/awesome-microvm/blob/main/examples/durable-sdk-worker/app.py), a Python copy of the suite's `microvm-app.ts` with the same routes and the same `succeed`, `fail` and `crash` modes, built it into a 1 GB image with the run, suspend, resume and terminate hooks enabled (2 minutes 6 seconds, a 504 MB memory snapshot), and pointed the SDK's runner at that image. The TypeScript durable function, the scenarios and the assertions stayed exactly as AWS wrote them. I ran them in three batches sized to my quota, and all 13 passed on the first run.

| Scenario | What it checks | Node worker | Python worker |
|---|---|---|---|
| succeed | run hook delivery, heartbeats, a 20 s job | 42 s | 35 s |
| fail | `MicrovmJobFailedError` carries the message | 43 s | 14 s |
| crash | `MicrovmTimeoutError` at the heartbeat timeout | 45 s | 33 s |
| terminated | the terminate hook fails the job well before the 300 s heartbeat timeout | 71 s | 23 s |
| request-succeed, request-fail | a named route, 10 KB of input over HTTP | 46 s, 38 s | 30 s, 20 s |
| auto-http, auto-http-cjk | input over the run hook limit goes over HTTP | 11 s, 36 s | 13 s, 18 s |
| run-hook-cjk, run-hook-emoji | 3,400 code points stay in the run hook, unchanged | 12 s, 14 s | 14 s, 16 s |
| session | the second job reads the first job's file | 16 s | 11 s |
| suspend-session | the worker suspends its own VM, memory survives the resume | 91 s | 98 s |
| parallel | two jobs in a `map` | 35 s | 11 s |

The Node column comes from the first run, with 13 scenarios competing for launches at 1 per second, and the Python column from batches, so the times compare the two workers only loosely. What the table shows is that every assertion held for both. The contract is small enough to implement in another language in an afternoon, and precise enough that the implementation passed a suite written for a different one.

## What I changed in microvm-ctl

Reading the worker's code changed three things in mine, all in microvm-ctl 0.5.0.

The first is a security fix. The MicroVM endpoint forwards any path to the VM for a caller holding an auth token, hook paths included. microvm-ctl's runtime acted on every call to `/aws/lambda-microvms/runtime/v1/terminate`, so anyone with a token for a VM could make it fail its own jobs. The Node worker acts on suspend, resume and terminate only when the call comes from Lambda itself, which the README documents as a `localhost` Host header and no `x-amzn-requestid`. microvm-ctl now does the same, and answers a forwarded hook call with 200 and nothing else.

The second is in part 4's durable lease. The worker's probes found that a closed callback can come back as `InvalidParameterValueException` or `ResourceNotFoundException` as well as `CallbackTimeoutException`. microvm-ctl treated only the last as closed and kept heartbeating a dead callback. It now stops on all three, and it retries a failed completion up to 5 times where it used to try once.

The third is the job contract itself, `HookApp.durable_job`, so a Python image built with microvm-ctl serves `microvm` and `microvmSession` with no other change. The same runtime still serves microvm-ctl's own leases for Step Functions, durable functions written in Python, SQS, EventBridge and HTTP, so one image can sit behind either.

## What I would change in the SDK

I have sent these to the team that wrote the packages, with the runs behind them.

The quota error is the one that matters. On a default account, 8 GB is eight 1 GB VMs, and image builds and suspended VMs count against it too. A `map` over 20 items, or two durable executions that overlap, will hit it, and today each job that misses fails in 2 seconds even though memory usually comes back within a minute or two. A durable function can wait for free, so an opt-in retry with a long backoff on `ServiceQuotaExceededException` would turn those failures into delays. microvm-ctl's `plan` goes one step further: it reads the applied quota from Service Quotas and sizes the concurrency, waves and worst-case cost before anything launches, which is the piece I offered to port to TypeScript.

Long jobs are hard to watch. `SendDurableExecutionCallbackHeartbeat` carries no payload, so for a 40-minute build the durable function knows only that the VM is alive. microvm-ctl keeps a small job record in the VM (phase, progress, counters and the last log lines) on `GET /status` and as server-sent events on `GET /events`. Something similar in the worker, such as `context.progress(done, total)`, would let an operator see where a job is without opening CloudWatch.

Python is half there. A Python worker package has started in the Python SDK's repository, with the job documents, the callback reporter and the heartbeat schedule done and the HTTP listener still to come, and there is no `microvm` operation for durable functions written in Python yet. The listener I tested is about 280 lines with a unit test for each behaviour the Node README specifies, so it can serve as a reference for that package.

The smaller points: a version check at the top of the e2e runner for AWS CLI 2.37 or later; one sentence on how the Dockerfile's `node:22-alpine` base relates to the `al2023` base image ARN the examples pass; and a way to find VMs whose terminate failed, which today run, and bill, until `maximumDurationInSeconds`, up to almost 8 hours for a long job. One gap belongs to the service: SuspendMicrovm authorizes on the image, so a VM allowed to suspend itself can suspend any VM from the same image, mid-job. The README says so and recommends one image per trust boundary. A condition key that limits the action to the caller's own VM would let self-suspend be on by default.

## The gotchas

- Use AWS CLI 2.37 or later for the e2e runner. Older v2 releases have no `lambda-microvms` command and no `--durable-config`.
- Size `map` concurrency to your memory quota. VM memory times concurrency must stay under it, with room for builds and suspended VMs. On 8 GB, the SDK's own suite needs batches of four or five.
- Leave the idle policy unset for `microvm` jobs. It counts only inbound endpoint traffic, and a running job receives none, so the VM would be suspended mid-job. Use `autoSuspendOnIdle` in a session instead.
- Give the VM's execution role `lambda:SendDurableExecutionCallbackSuccess`, `Failure` and `Heartbeat`, and `lambda:SuspendMicrovm` on the image ARN if the session suspends itself.
- Give the function `lambda:TerminateMicrovm`. Without it every operation still returns its result, and every VM runs until its timeout plus 5 minutes.
- Count the run hook limit in code points. 3,400 CJK characters or emoji fit; byte-counting code would send them over HTTP for no reason, or truncate them.
- Accept a second delivery of the same callback id and ignore it. A replayed request step can POST the same job twice.
- In a Python image, act on the suspend, resume and terminate hooks only for calls from Lambda. The endpoint forwards hook paths from anyone holding an auth token.

## Which one, when

| You need | Use | Why |
|---|---|---|
| One job in its own kernel from a durable function in TypeScript | `microvm` with the Node worker | Five durable operations in one call, idempotent launch, terminate in every branch |
| Several jobs on one VM, with waits between them | `microvmSession` with `autoSuspendOnIdle` | Files and memory survive between jobs; the VM pays storage only while it waits |
| The same, with the job code in Python | `microvm` or `microvmSession`, and an image built with microvm-ctl 0.5.0 | `HookApp.durable_job` passed the SDK's suite unchanged |
| A durable function written in Python | microvm-ctl's `lease_microvm` and `lease_map`, from part 4 | No Python `microvm` operation exists yet |
| Step Functions, SQS, EventBridge or your own controller | microvm-ctl leases, from part 4 | One image completes any of six lease kinds |
| A fan-out larger than your quota | microvm-ctl's `plan`, then `lease_many` or a sized `map` | The plan refuses or splits into waves before anything launches |

## What to run

```sh
pip install "microvm-ctl>=0.5"
mvm bootstrap
git clone https://github.com/Vivek0712/awesome-microvm && cd awesome-microvm
python examples/durable-sdk-worker/build.py           # the Python worker image, about 2 minutes

git clone https://github.com/aws/aws-durable-execution-sdk-js && cd aws-durable-execution-sdk-js
npm ci
for p in aws-durable-execution-sdk-js aws-durable-execution-sdk-js-testing \
         aws-durable-execution-sdk-js-extras aws-durable-execution-sdk-js-microvm-worker; do
  npm run build -w packages/$p; done
npm test -w packages/aws-durable-execution-sdk-js-extras
npm test -w packages/aws-durable-execution-sdk-js-microvm-worker
cd packages/aws-durable-execution-sdk-js-extras
AWS_REGION=us-east-1 E2E_SCENARIOS=succeed,fail,crash,terminated node e2e/run-e2e.mjs
# for the Python worker, change the image ARN the runner passes as MICROVM_IMAGE_ARN to the one build.py printed
```

I am an AWS AI Hero, and the question I hear most often about MicroVMs is how to hand one a job without running a server to wait for it. Part 4 answered it with a lease I built in Python. The answer is now in the AWS SDK for TypeScript too, and both speak a contract the same image can serve. Part 1 of this series has the control plane, part 2 the seven workloads, part 3 the multi-tenant agents, part 4 the lease from any orchestrator, and part 5 Strands Box inside the VMs.
