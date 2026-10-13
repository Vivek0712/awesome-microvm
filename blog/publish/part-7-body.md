Part 4 of this series handed a task to a Lambda MicroVM from four kinds of orchestrator: Step Functions, a durable function, a queue and a controller of your own. Each of them gave the VM a lease in `runHookPayload`, and the VM completed the lease itself when the job ended. One kind of lease in microvm-ctl has no AWS service behind it. The `http` lease gives the VM a URL and a token, and the VM POSTs its heartbeats and its result to that URL. Until this week that URL had to be a server I ran somewhere.

[Lambda Web Functions](https://aws.amazon.com/about-aws/whats-new/2026/10/aws-lambda-web-functions/) are a new kind of Lambda function: a Node HTTP server that Lambda runs, scales and puts behind an HTTPS endpoint of its own. One environment serves many requests at once, a request can stream its response for up to 15 minutes, and there is no API Gateway or function URL in front. That is the shape the `http` lease wants on the other end. So I built one Web Function that takes a job over HTTP, launches a MicroVM for it with a lease that points back at the same function, streams the VM's log to the caller while it runs, records the result the VM sends, and terminates the VM.

< FIGURE 1 of 2: upload blog/img/hook-06-web-handoff.png here >
Alt text: A Lambda Web Function as the front door and the orchestrator: a caller POSTs a job to /jobs, the Web Function spends one launch from an hourly budget in DynamoDB, writes the job record with a hash of a fresh lease token, and calls RunMicrovm with an http lease whose target is its own /callbacks/id; the MicroVM, the unchanged handoff-agent-small image from part 4, runs the steps and POSTs heartbeats and its result to /callbacks/id with the lease token; the Web Function proxies the VM's /events to the caller as one streamed response, records the outcome once, and calls TerminateMicrovm.
*Measured live in us-east-1 on 2026-10-13.*

This is part 7 of Building on AWS Lambda MicroVMs. The code is in [awesome-microvm/examples/web-handoff](https://github.com/Vivek0712/awesome-microvm/tree/main/examples/web-handoff): the function, a small page to try it from, unit tests, the live test script and a deploy script.

## What a Web Function is, for this purpose

Three properties decide how it fits with MicroVMs.

It is a long-lived process. The server starts once per environment and handles concurrent requests until Lambda retires the environment, so a request can hold a stream open while other requests, including the VM's callbacks, are served by the same process or by another environment.

It runs Node and only Node. microvm-ctl is Python, so the launch side here is about 50 lines of JavaScript on `@aws-sdk/client-lambda-microvms`. The payload those lines send is microvm-ctl's lease format, so the VM side needs nothing new.

Its environments can go away at any time and a request lasts at most 900 seconds. Nothing about a job can live only in memory. Job state goes in DynamoDB, the VM owns the job while it runs, and the function only records what the VM reports.

Writing one is writing a Node server that listens on the address Lambda gives it:

```javascript
const [host, port] = process.env.AWS_LAMBDA_HTTP_ENDPOINT.split(":");
server.listen(Number(port), host);
process.on("SIGTERM", () => process.exit(0));
```

Deploying it is one command with AWS CLI 2.37 or later, which uploads the bundle, creates the function and gives it an endpoint:

```sh
aws lambda-web deploy --name microvm-web-handoff --code dist --create \
  --execution-role-arn arn:aws:iam::<account>:role/microvm-web-handoff-fn \
  --timeout-seconds 900 --max-concurrency-per-environment 64 \
  --env MICROVM_IMAGE_ARN=<image arn> --env JOBS_TABLE=microvm-web-handoff-jobs --env DEMO_KEY=<key>
```

## The handoff

`POST /jobs` takes a list of shell steps. The function checks the caller's key, spends one launch from an hourly budget, writes the job record, and launches the VM:

```javascript
const id = randomBytes(9).toString("base64url");
const token = randomBytes(32).toString("hex");
const payload = JSON.stringify({
  lease: { kind: "http", token, region, target: `${selfUrl}/callbacks/${id}`, heartbeat_s: 10, id },
  task: { steps },
});
await db.send(new PutCommand({ TableName, Item: { id, state: "launching", tokenHash: sha256(token), steps } }));
const vm = await vms.send(new RunMicrovmCommand({
  imageIdentifier: IMAGE,
  runHookPayload: payload,
  clientToken: sha256(`http:${token}:${id}`),                 // a retried launch returns the same VM
  maximumDurationInSeconds: 360,
  ingressNetworkConnectors: [connector("ALL_INGRESS")],        // so the function can stream the VM's /events
  egressNetworkConnectors: [connector("INTERNET_EGRESS")],     // so the VM can reach /callbacks
}));
```

Inside the VM, microvm-ctl's hook runtime reads the lease from the run hook, runs the steps, and calls the target. It POSTs `{"status": "heartbeat"}` every 10 seconds while a step runs, and `{"status": "success", "result": ...}` or `{"status": "failure", "error": ...}` at the end, each with `Authorization: Bearer <lease token>`. An answer of 404 or 410 tells the VM the lease is closed, and it stops.

`POST /callbacks/<id>` is the other half. It hashes the bearer token and compares it with the stored hash, so the token itself is never written down. A heartbeat updates `lastHeartbeat`. A result goes through one conditional update that moves the job to `succeeded` or `failed` only if it is not already final, so the first final write wins, and only the request that won it calls TerminateMicrovm. The same function then answers 410 to anything that arrives later.

The caller chooses how to wait. With `?stream=1`, the response to `POST /jobs` stays open as server-sent events: a `launched` event with the VM id, the VM's own log events while it runs, and a `result` event with the job record at the end. For the log, the function mints a MicroVM auth token for port 8080 and proxies the VM's `/events`, the same job telemetry `mvm watch` reads. Without `stream`, the caller gets 202 with the job id at once and polls `GET /jobs/<id>`, which is the only option for a job longer than the 900 second request limit.

< FIGURE 2 of 2: upload blog/img/demo-web-handoff.png here >
Alt text: A streamed job from curl: the launched event carries the MicroVM id and a 159 ms RunMicrovm call; a snapshot event from the VM shows the lease accepted and steps 1 and 2 started; log events follow as each step finishes, aarch64, 1 CPU, then tick 1 to 3; the result event shows the job succeeded with each step's exit code and duration, 9 seconds from the request to the end of the stream.
*One streamed job against the deployed function, recorded on 2026-10-13. The snapshot frames are shortened.*

## The VM image did not change

The VM is `handoff-agent-small`, the 512 MiB image I built for part 4 with `mvm image build handoff-agent-small examples/handoff-agent --memory 512`. In part 4 that same image was leased by Step Functions, by a durable function and by microvm-ctl's own fleet. Here it is leased by a Node server that did not exist when I built it. microvm-ctl needed no change either. That is the point of putting the contract in the run hook: the orchestrator can change without touching the image.

## Running it

I ran seven scenarios against the deployed endpoint with `test/live.mjs`. Every VM ended `TERMINATED`.

| Scenario | What happened |
|---|---|
| A streamed job of three steps | Succeeded. RunMicrovm returned in 123 ms, the first log line reached the client at 2.1 s, the stream ended at 7.9 s |
| 202, then poll | Succeeded. The 202 came back in 0.8 s, the job finished at 7.8 s |
| A step exits with code 3 | Failed with `StepFailed`, with the failing step, its exit code and the earlier steps' output in the record |
| A 30 second job | Succeeded with 2 heartbeats recorded |
| The VM terminated from outside mid-job | The VM's terminate hook failed the lease with `Terminated`, marked retryable; the function recorded it 1.6 s after the TerminateMicrovm call |
| Four jobs at once | 4 VMs, all succeeded, 5.9 s for all four |
| No key, empty steps, a forged callback token | 401, 400 and 403; the real job behind the forged callback still succeeded |

The terminate case is the one I care most about. Nothing in the function watched for it. The VM saw its own terminate hook, failed the lease, and the callback arrived before the VM was gone. A job that dies without any word, because the VM crashed or lost its network, is caught differently: when a job's last heartbeat is more than three intervals plus 30 seconds old, the next read of it marks it `lost` and terminates the VM. I could not trigger that path live. In this image the hook runtime is PID 1, which a job cannot kill, and `/proc` is read-only, so a job cannot crash the kernel either. The unit tests cover it with a fake clock.

There are 8 unit tests, with in-memory stand-ins for DynamoDB and the MicroVMs client: the lease payload and its 4,096 code point limit, the key and step validation, the RunMicrovm request, heartbeat and completion with a wrong token and a late one, a typed failure, the `lost` path, the launch budget, and a quota error at launch.

## Keeping a public endpoint from becoming a bill

A Web Function endpoint is public, and every accepted job starts a VM. I put five limits on it.

- Every job route needs an `x-demo-key` header. The callback route needs the job's lease token instead, which the caller never sees.
- An hourly launch budget in DynamoDB, 30 by default, is spent with a conditional update before RunMicrovm, so the 31st request in an hour gets 429 and no VM.
- Each VM gets `maximumDurationInSeconds` of the job budget plus a minute, and an idle policy that suspends it without auto-resume, so a VM nobody hears from still ends.
- A job that stops heartbeating is marked `lost` and its VM terminated.
- The endpoint itself is limited to 2 environments and 100 requests a second with `update-web-function-endpoint`.

## The gotchas

- The 900 second request limit covers a whole streamed response. Stream jobs that fit, and give longer ones the 202 and let the callback record them.
- The VM has to reach the function, so the endpoint uses `ApplicationManaged` auth and the lease token is the credential on `/callbacks`. An `IamAuth` endpoint would need the VM to sign each callback with SigV4.
- The endpoint `rateLimit` accepts 0 or a multiple of 100 up to 1,000. 100 is the lowest cap that still lets traffic through.
- Environments are shared and can disappear, and a callback can land on a different environment than the one streaming the job. Keep every piece of job state in a store, and make the final write conditional.
- Streaming the VM's `/events` needs `ALL_INGRESS` on the VM and an auth token minted for each stream. A job that only reports through the callback needs neither.
- The callback can arrive before RunMicrovm returns for a fast job. Move the record from `launching` to `running` with a condition, so the update does not overwrite a result.
- The `aws lambda-web` commands need AWS CLI 2.37 or later.

## Which one, when

| You need | Use | Why |
|---|---|---|
| An HTTP API that runs each request's job in its own kernel and streams the output | A Web Function with an `http` lease, this part | One endpoint for callers and for the VM, no servers, the log streamed live |
| A job of more than 15 minutes behind an HTTP API | The same function, with the 202 and polling | The callback records the result whenever it comes |
| Retries, waits and steps between jobs | A durable function with `microvm`, or a microvm-ctl lease, parts 4 and 6 | The durable SDK replays the workflow; the Web Function only records one job |
| A state machine or a queue in front | Step Functions, SQS or EventBridge leases, part 4 | The same image completes all of them |

## What to run

```sh
pip install "microvm-ctl>=0.5"
mvm image build handoff-agent-small examples/handoff-agent --memory 512
cd examples/web-handoff
npm ci && npm test
DEMO_KEY=<pick a key> ./infra/deploy.sh
curl -N -X POST "https://<endpoint>/jobs?stream=1" -H "x-demo-key: <key>" \
  -d '{"steps": ["uname -m", "for i in 1 2 3; do echo tick $i; sleep 1; done"]}'
WEB_URL=https://<endpoint> DEMO_KEY=<key> node test/live.mjs
```

I am an AWS AI Hero, and each new way to run code on Lambda gets the same test from me: whether it can hand a job to a MicroVM with nothing waiting in between. Web Functions pass it, and the image from part 4 did not need to know. Part 1 of this series has the control plane, part 2 the seven workloads, part 3 the multi-tenant agents, part 4 the lease from any orchestrator, part 5 Strands Box inside the VMs, and part 6 the MicroVM operations in the AWS durable execution SDK.
