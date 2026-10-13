# Web Function to MicroVM handoff

A [Lambda Web Function](https://docs.aws.amazon.com/lambda/latest/dg/web-functions.html) is the front door and the orchestrator; a Lambda MicroVM does the work. The Web Function launches each job's VM with a microvm-ctl HTTP lease in `runHookPayload` whose target is the Web Function's own endpoint. The VM heartbeats and completes the lease by POSTing to `/callbacks/<id>` with the lease token; the Web Function records the outcome in DynamoDB and terminates the VM. A caller either waits on one streamed response, which carries the VM's live log proxied from its `/events`, or takes a 202 and polls.

```
browser / curl --POST /jobs?stream=1--> Web Function (Node 24) --RunMicrovm(lease: kind http, target: <self>/callbacks/<id>)--> MicroVM
      ^                                   |   ^                                                                             |
      |  SSE: launched, log..., result    |   +---- POST /callbacks/<id>  Bearer <lease token>: heartbeat, success, failure -+
      +-----------------------------------+   |
                                              +--> DynamoDB (job record, hourly launch budget)  --> TerminateMicrovm
```

The VM side is unchanged: it is the `handoff-agent-small` image from [handoff-agent](../handoff-agent), the same one Step Functions and durable functions lease in part 4. Web Functions run Node only, so the launch side is about 50 lines of JavaScript on `@aws-sdk/client-lambda-microvms` instead of microvm-ctl's Python `FleetManager.lease`; the payload is the same lease contract.

| Path | Role |
|---|---|
| `src/server.mjs` | the Web Function: `POST /jobs`, `GET /jobs/:id`, `GET /jobs/:id/events`, `POST /callbacks/:id`, the page at `/` |
| `src/page.mjs` | a small page to submit steps and watch the VM's log stream in |
| `test/server.test.mjs` | unit tests with in-memory DynamoDB and MicroVMs fakes (`npm test`) |
| `test/live.mjs` | the live scenarios below, against a deployed endpoint; writes `results/` |
| `infra/deploy.sh` | jobs table, execution role, `aws lambda-web deploy`, endpoint limits |

## Run it

```console
mvm image build handoff-agent-small examples/handoff-agent --memory 512    # once, if you do not have it
cd examples/web-handoff
DEMO_KEY=<pick a key> ./infra/deploy.sh          # AWS CLI 2.37 or later, Node 22 or later
curl -N -X POST "https://<endpoint>/jobs?stream=1" -H "x-demo-key: <key>" \
  -d '{"steps": ["uname -m", "for i in 1 2 3; do echo tick $i; sleep 1; done"]}'
WEB_URL=https://<endpoint> DEMO_KEY=<key> AWS_PROFILE=<profile> node test/live.mjs
```

## Measured

Live in us-east-1 on 2026-10-13, account quotas RunMicrovm 1/s and 8 GB, 512 MiB VMs. Times are from the client.

| Scenario | Outcome | Numbers |
|---|---|---|
| streamed job, 3 steps | succeeded, log streamed live | RunMicrovm 123 ms, first log line 2.1 s, done 7.9 s |
| 202 then poll | succeeded | accepted in 0.8 s, done 7.8 s |
| a step exits 3 | failed with `StepFailed`, the step and its output in the record | done 2.9 s |
| 30 s job | succeeded, 2 heartbeats recorded | 32.5 s |
| VM terminated from outside mid-job | failed with `Terminated` (retryable), sent by the VM's terminate hook | recorded 1.6 s after TerminateMicrovm |
| four jobs at once | 4 VMs, all succeeded | 5.9 s wall for all four |
| no key / empty steps / forged callback token | 401 / 400 / 403; the real job still succeeded | |

Every VM ended `TERMINATED`. The raw results are in `results/`.

## Guardrails

- `x-demo-key` on every job route; callbacks need the job's lease token, of which only a SHA-256 is stored.
- An hourly launch budget in DynamoDB (`LAUNCHES_PER_HOUR`, default 30) refuses with 429 before RunMicrovm.
- Each VM gets `maximumDurationInSeconds` = budget + 60 s, and an idle policy with no auto-resume.
- A job whose VM stops heartbeating for 3 intervals plus 30 s is marked `lost` on its next read, and its VM terminated.
- The endpoint has `maxEnvironments` 2 and `rateLimit` 100.

## Gotchas

- A Web Function request lasts at most 900 s, and the limit covers a whole streamed response. Stream jobs that fit; give longer ones the 202 and let the callback record them.
- The VM must be able to reach the Web Function, so the endpoint stays `ApplicationManaged` for `/callbacks` and the lease token is the credential. An `IamAuth` endpoint would need the VM to sign requests with SigV4.
- The endpoint's `rateLimit` takes 0 or multiples of 100 up to 1,000, so 100 is the lowest non-zero cap.
- Environments are shared by concurrent requests and can go away at any time, so job state lives in DynamoDB, and a callback can land on a different environment than the one streaming the job.
- Proxying the VM's `/events` needs `ALL_INGRESS` on the VM and an auth token minted per stream.
- In the default image the hook runtime is PID 1 and `/proc` is read-only, so a job cannot kill its own runtime to simulate a crash. The `lost` path is covered by the unit tests.
