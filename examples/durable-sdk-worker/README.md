# A Python worker for the AWS durable execution SDK's MicroVM operations

`@aws/durable-execution-sdk-js-extras` has two MicroVM operations for Lambda durable functions written in TypeScript, `microvm` and `microvmSession`, and a Node worker for the code inside the VM. This example is the other side of that contract in Python, on microvm-ctl's hook runtime (`HookApp.durable_job`, microvm-ctl 0.5.0 or later). The TypeScript durable function does not change; only the image does.

`app.py` mirrors the SDK's own end-to-end app (`packages/aws-durable-execution-sdk-js-extras/e2e/microvm-app.ts`) route for route, so the SDK's e2e suite runs against it unchanged. On 2026-10-13 it passed 13 of 13, in us-east-1, against aws-durable-execution-sdk-js at commit 571858c7. The write-up is part 6 of the Building on AWS Lambda MicroVMs series ([blog/05-durable-sdk.md](../../blog/05-durable-sdk.md)).

| Path | Role |
|---|---|
| `app.py` | the job handler (`succeed`, `fail` and `crash` modes) on the default route and `/job`, plus `/write`, `/read`, `/remember` and `/recall` for the session scenarios |
| `Dockerfile` | `al2023-minimal`, Python 3.12, boto3 1.43.60 or later (durable callback APIs and `SuspendMicrovm`) |
| `build.py` | builds the image at 1 GB with the run, suspend, resume and terminate hooks enabled |

## Run it

```console
pip install "microvm-ctl>=0.5"
mvm bootstrap                                       # once: bucket and build role
python examples/durable-sdk-worker/build.py         # about 2 minutes; prints the image ARN

git clone https://github.com/aws/aws-durable-execution-sdk-js && cd aws-durable-execution-sdk-js
npm ci && for p in aws-durable-execution-sdk-js aws-durable-execution-sdk-js-testing \
  aws-durable-execution-sdk-js-extras aws-durable-execution-sdk-js-microvm-worker; do npm run build -w packages/$p; done
cd packages/aws-durable-execution-sdk-js-extras
# AWS CLI 2.37 or later on PATH. run-e2e.mjs builds and uses its own Node image; to point it at this one,
# change the image ARN it passes as MICROVM_IMAGE_ARN to the ARN printed above.
AWS_REGION=us-east-1 E2E_SCENARIOS=succeed,fail,crash,terminated,request-succeed node e2e/run-e2e.mjs
```

On an account with the default 8 GB of MicroVM memory, run the scenarios in batches of four or five. Quota errors are not retried by the SDK's launch step, so a batch that does not fit fails in about 2 seconds.

With the suspend scenario the VM suspends itself, so its execution role needs `lambda:SuspendMicrovm` on the image ARN, as well as `lambda:SendDurableExecutionCallbackSuccess`, `Failure` and `Heartbeat`.
