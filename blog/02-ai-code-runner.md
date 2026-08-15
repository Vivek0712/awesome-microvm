# The blast radius of model-written code is one microVM

*An LLM writes Python, a Firecracker VM runs it, and tracebacks feed back to the model until the script exits 0 — a self-repair loop that lives entirely inside a disposable AWS Lambda MicroVM.*

Every agentic coding product has the same uncomfortable step: an LLM emits code nobody has reviewed, and something has to execute it. That something usually shares a kernel, a filesystem, or a credential set with things you care about. In this post we put the whole loop — Bedrock call, code execution, traceback, retry — inside one Lambda MicroVM. If the model writes `shutil.rmtree("/")`, it destroys a Firecracker VM we were going to terminate anyway.

This is post 02 in the [awesome-microvm](https://github.com/vivekrajaps/awesome-microvm) series. Everything here was run against the live service in `us-east-1` in August 2026.

## Why a microVM (and not a container or a Lambda function)

For untrusted, model-written code the isolation question comes first:

- **A container** shares the host kernel — and a code-execution product is exactly the workload that invites escapes, because the attacker gets to *write the payload* by prompting your model. A Firecracker microVM gives the code its own kernel; that is the same boundary AWS uses to separate Lambda tenants.
- **A Lambda function** has the right isolation but the wrong shape. The self-repair loop is stateful and iterative: generated files stay in the workspace across attempts, and a session may spread over minutes or hours of human back-and-forth. Lambda's execution model (short, stateless invocations, no suspend) fights that; a microVM with suspend/resume matches it.
- **An EC2 instance** has the isolation and the statefulness, but boots in minutes and bills whether the agent is thinking or not. Our microVMs go from API call to serving authenticated traffic in p50 3.54 s (p95 4.49 s), and a suspended VM costs storage only — $0.08/GB-month on a 0.61 GB snapshot.

The decision rationale for this use case in one line: kernel-level isolation per session, a persistent workspace within the session, and a per-second bill that goes to near-zero when the human walks away.

## Architecture

```mermaid
flowchart LR
    subgraph control["Control plane (mvm CLI)"]
        IB[ImageBuilder\nzip → S3 → ACTIVE image]
        FM[FleetManager\nrun / suspend / resume / terminate]
        TK[Token minting\nCreateMicrovmAuthToken → JWE]
    end
    subgraph vm["Execution plane — one microVM per session"]
        HA[HookApp :8080\n/ready /run /resume]
        SOLVE[POST /solve loop]
        WS[(/tmp/workspace\nsolution.py + artifacts)]
        HA --> SOLVE --> WS
    end
    BR[Amazon Bedrock\nnova-lite]
    CLIENT[Caller] -- "X-aws-proxy-auth (JWE)" --> HA
    FM --> vm
    IB --> FM
    TK --> CLIENT
    SOLVE -- "converse() via execution role" --> BR
```

Two planes. The control plane builds the image, launches/suspends/terminates VMs, and mints the port-scoped JWE auth tokens (1–60 min TTL) callers must present as `X-aws-proxy-auth` at the VM's dedicated endpoint — there is no load balancer, each VM gets `<id>.lambda-microvm.us-east-1.on.aws`. The execution plane is a single stdlib-only Python server inside the VM that answers the platform's lifecycle hooks (`/ready`, `/run`, `/resume`) and one application route, `POST /solve`, where the entire generate→execute→repair loop runs. The only thing that leaves the VM during a session is the Bedrock API call.

## Build it

The image is deliberately boring — a Python 3.12 toolchain the model's code is likely to want:

```dockerfile
FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y python3.12 python3.12-pip && dnf clean all
RUN python3.12 -m pip install --no-cache-dir boto3 numpy pandas matplotlib

WORKDIR /app
COPY microvm_hooks.py app.py /app/

EXPOSE 8080
ENTRYPOINT ["python3.12", "/app/app.py"]
```

```console
$ mvm build ai-code-runner examples/ai-code-runner/ --memory 2048
```

The build ran in 144.6 s and produced a 608 MB memory snapshot plus a 24 MB disk snapshot. That memory snapshot is the detail that shapes the whole application design: every VM you launch is a *restored clone of the build VM's RAM*. Whatever existed in memory at snapshot time — random-number generator state, open sockets, and, if you were careless, credentials — is duplicated into every clone.

Which is why `app.py` refuses to create its Bedrock client at import time:

```python
_bedrock = None  # created lazily in /run — never at build time (dead conns in snapshot)

@app.on_run
def on_run(_ctx):
    global _bedrock
    import boto3
    _bedrock = boto3.client("bedrock-runtime",
                            region_name=os.environ.get("AWS_REGION", "us-east-1"))

@app.on_resume
def on_resume(ctx):
    on_run(ctx)  # refresh the client: pre-suspend connections may be dead
```

Three hooks, three reasons:

- **`/ready`** creates `/tmp/workspace` and returns `True`; the platform only snapshots after `/ready` returns 200, so this is your last chance to shape what gets cloned.
- **`/run`** fires on each freshly launched clone. *This* is where the boto3 client is born, so its credentials come from the VM's execution role — resolved at runtime, per-VM, via the instance metadata service. Nothing secret is ever baked into the image, because a snapshot turns RAM into stored data. An API key held in a Python variable at build time would ship, at rest, inside every copy of a 608 MB file.
- **`/resume`** re-runs the same initialization, because a client that survived suspend is holding TCP connections that died while the VM was frozen.

The loop itself is 20 lines. Generate a script, run it as a subprocess in the workspace, and if it exits non-zero, hand the traceback straight back to the model:

```python
for i in range(int(body.get("max_iterations", 3))):
    code = _generate(messages)          # Bedrock converse(), nova-lite, temp 0.2
    result = _execute(code)             # python3.12 solution.py, 60 s timeout
    if result["exit_code"] == 0:
        return 200, {"solved": True, "iterations": i + 1, "code": code,
                     "stdout": result["stdout"], "attempts": attempts,
                     "artifacts": sorted(os.listdir(WORKSPACE))}
    messages.append({"role": "assistant", "content": [{"text": code}]})
    messages.append({"role": "user", "content": [
        {"text": f"That failed:\n{result['stderr'][-3000:]}\nFix it. Full script only."}
    ]})
```

Note what we *don't* do: no sandboxing inside the VM, no import allowlist, no seccomp gymnastics. The subprocess runs with a 60-second timeout and otherwise full freedom, because the VM boundary is the sandbox. Anything the code writes — a plot from matplotlib, a CSV from pandas — lands in `/tmp/workspace` and is reported back in the `artifacts` list, and it persists there across `/solve` calls and across suspend/resume for the life of the session.

## Run it

Here is the actual captured transcript:

![ai-code-runner demo transcript](../benchmarks/results/demo-ai-code-runner.svg)

The VM went from `mvm run ai-code-runner --wait` to serving in 15.7 s in this capture (`--wait` polls conservatively; the measured p50 from launch to first authenticated byte is 3.54 s). We posted the task *"Compute the first 8 Fibonacci numbers and print them as a Python list"*, and one `POST /solve` returned:

```json
{
  "solved": true,
  "iterations": 1,
  "stdout": "[0, 1, 1, 2, 3, 5, 8, 13]\n",
  "attempts": [{"iteration": 1, "exit_code": 0, "duration_ms": 1250.9}],
  "artifacts": ["solution.py"]
}
```

Nova-lite's first draft — a plain `fibonacci(n)` with a while-loop, visible in full in the `code` field of the transcript — ran clean in 1250.9 ms of subprocess time, so the repair loop never had to fire. When a first draft *is* broken, the traceback round-trips through `messages` and the next attempt runs clean; either way, execution-plane overhead per round trip is about 111 ms (warm p50, including TLS, proxy auth, and the subprocess), so the model's thinking time dominates. The workspace ends the session holding `solution.py`, and the VM is terminated.

One honest aside: in an earlier capture we launched this VM *without* its execution role attached, and the very first `converse()` call threw `botocore.exceptions.NoCredentialsError` instantly. That failure is the design working — there was no fallback key to find, not in an env var, not anywhere in 608 MB of cloned RAM, because we never put one there. A missing role fails as a clean traceback, never as a silently shared credential.

## What it costs

Rates (us-east-1, per-second billing, vCPU fixed at memory/2):

| Item | Rate |
|---|---|
| vCPU | $0.0000276944 /vCPU-s |
| Memory | $0.0000036667 /GB-s |
| Snapshot write / read | $0.0038 / $0.00155 per GB |
| Suspended + image storage | $0.08 /GB-month |

Worked example from our CostModel for this exact shape (2 GB / 1 vCPU, 0.61 GB measured snapshot). An agentic coding session is bursty: minutes of active generate-run-fix, then hours where the human is reviewing, meeting, or asleep — and the workspace must survive.

| Session shape | MicroVM | Always-on 2 GB container | Saved |
|---|---|---|---|
| One-shot solve, terminate (8 s) | $0.0003 | — | — |
| 30 min active + 8 h suspended | $0.0669 | $1.0719 | 93.8% |
| 2 h active + 22 h suspended | $0.2602 | $3.0264 | 91.4% |

A suspend/resume cycle on the 0.61 GB snapshot costs about $0.0034 in snapshot write+read, so don't reflexively suspend a job you'll never come back to — terminate one-shots. And if your workload is genuinely 24/7 busy, an always-on VM is ~$3.03/day and Fargate wins; suspend economics are the whole argument here. Bedrock tokens are billed separately by that service — nova-lite sits at the cheap end, which is why it's the default `MODEL_ID`.

## The gotchas

- **Snapshots turn RAM into stored data.** This bit us in design, not production, because we planned for it: no client construction at build time, secrets only via the execution role in `/run`, RNG reseeded by the hook server on every launch. Audit anything your framework initializes at import time.
- **Resumed connections are dead.** `/resume` must rebuild the Bedrock client; a suspended VM's TCP connections do not survive the freeze. Our `on_resume` just calls `on_run` again.
- **No execution role, no Bedrock — loudly.** Our earlier mis-launched capture showed exactly this. Treat `NoCredentialsError` from inside the VM as "check the role on `RunMicrovm`", not as a reason to bake in a key.
- **New-account quotas will surprise you.** Our fresh account had an applied memory quota of 8 GB (published default: 1024 GB) that counts RUNNING, SUSPENDED, *TERMINATING*, and image-build VMs, plus a RunMicrovm rate of 1/s. A per-session-VM product hits both immediately. Our FleetManager reads applied quotas at startup and throttles to 80%; our RunMicrovm raise from 1→5 was auto-approved via a single API call. Request raises on day one.
- **Sessions have a ceiling.** Total VM lifetime maxes out at 8 h (28,800 s). A long-running coding session needs a plan for checkpointing the workspace out (S3) and re-launching.

## Take it further

- **Fan out verification.** Launch N clones of this image and have each attempt the same task independently — post 03 (agent-eval) does exactly this with a fan-out harness, and clones from one snapshot make N-way sampling cheap.
- **Ship artifacts to S3 from inside the VM.** The endpoint has a bandwidth cap (1 MB/s at 0.5 GB, up to 16 MB/s at 8 GB); a generated dataset or plot bundle should leave via the execution role and S3, not through the proxy.
- **Swap the model per deployment.** `MODEL_ID` is an image-level env var — but remember env vars are shared by every clone of the image, so per-tenant configuration belongs in `runHookPayload` (see post 08, multi-tenant-agents).

---

This example and all eight images: [awesome-microvm](https://github.com/vivekrajaps/awesome-microvm) · the plane and benchmark harness: [microvm-ctl](https://github.com/vivekrajaps/microvm-ctl) (`pip install microvm-ctl`)

*Series: [00 — Control and scale MicroVMs like a pro](00-control-and-scale-microvms-like-a-pro.md) · [01 — Code sandbox](01-code-sandbox.md) · **02 — AI code runner** · [03 — Agent eval](03-agent-eval.md)*
