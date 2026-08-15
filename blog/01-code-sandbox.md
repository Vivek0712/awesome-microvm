# Build your own E2B on Lambda MicroVMs

*A per-session Python sandbox for untrusted and AI-generated code — kernel isolation, state that survives across calls, serving in 3.5 s from a snapshot — measured live on `us-east-1`.*

Every AI product that runs model-generated code — Perplexity's analysis mode, v0's previews, any agent with a "run Python" tool — pays someone for the same primitive: a hardware-isolated VM per session, booted in seconds, with a filesystem and pip environment that persist between calls. E2B and Vercel Sandbox built businesses on exactly this. AWS Lambda MicroVMs now sells the raw primitive directly, and in this post we build the sandbox on top of it: a p50 of **3.54 s** from API call to serving authenticated traffic, **111 ms** per warm execution, and **$0.0003** for an 8-second one-shot job.

This is the first use-case post in the [awesome-microvm](https://github.com/vivekrajaps/awesome-microvm) series. The [flagship post](00-control-and-scale-microvms-like-a-pro.md) covers the control plane; here we build one thing well.

## Why a microVM and not a container or a Lambda function

The sandbox contract has three requirements, and each one eliminates an alternative.

**Untrusted code needs a kernel boundary.** LLM-generated Python will eventually emit `ctypes` tricks, fork bombs, or a container escape it read about in its training data. Containers share the host kernel; a seccomp profile is a filter, not a wall. Lambda MicroVMs run each session in its own Firecracker VM — the same isolation Lambda itself has used for years, now with the controls exposed.

**Sessions need state.** An agent runs a cell, inspects the output, writes a file, `pip install`s a library, runs another cell. A Lambda function's execution environment is recycled on its own schedule and can't hold a guaranteed per-session filesystem across invocations. A microVM is *yours* until you terminate it: files under `/tmp/workspace` and packages installed at runtime persist across every call, and even across suspend/resume — we measured a resumed VM coming back as **the same PID 1** with its execution counter and workspace file intact.

**Sessions are bursty.** An always-on 2 GB container per user costs ~$3.03/day whether they run code or not. The snapshot-launch model means a VM exists only while a session does — and per-second billing makes short sessions almost free.

## Architecture

```mermaid
flowchart LR
    subgraph cp["Control plane"]
        B["mvm image build<br/>Dockerfile → snapshot"]
        R["mvm run / terminate<br/>one VM per session"]
        T["CreateMicrovmAuthToken<br/>port-scoped JWE"]
    end
    subgraph vm["Execution plane — one Firecracker VM per session"]
        H["HookApp (stdlib only)<br/>/ready /validate /run /suspend"]
        E["POST /execute<br/>POST /pip<br/>GET /state"]
        W["/tmp/workspace<br/>+ runtime pip installs"]
    end
    A["Agent / product backend"] -->|"X-aws-proxy-auth"| E
    B --> R
    R --> H
    T --> A
    E --> W
```

Two planes. The control plane builds the image once (your Dockerfile is executed on a build VM, then the *running* process is snapshotted), launches one VM per session, and mints the JWE tokens your backend attaches as `X-aws-proxy-auth`. The execution plane is the app inside the VM: a zero-dependency HTTP server that answers the service's lifecycle hooks and exposes the three routes a sandbox needs. Each VM gets its own dedicated HTTPS endpoint — there is no load balancer, which is fine here, because a session maps 1:1 to a VM anyway.

## Build it

The whole image is a 13-line Dockerfile ([examples/code-sandbox/Dockerfile](../examples/code-sandbox/Dockerfile)):

```dockerfile
FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y python3.12 python3.12-pip && dnf clean all
RUN python3.12 -m pip install --no-cache-dir numpy pandas requests

WORKDIR /app
COPY microvm_hooks.py app.py /app/

EXPOSE 8080
ENTRYPOINT ["python3.12", "/app/app.py"]
```

Everything installed here is baked into the memory snapshot that every session clones — which is why numpy and pandas cost nothing at session start. The code-sandbox image builds in **123.4 s** and produces a **609 MB** memory snapshot plus a **22 MB** disk snapshot.

The app ([examples/code-sandbox/app.py](../examples/code-sandbox/app.py)) wires four lifecycle hooks, and each one earns its place in this use case:

```python
@app.on_ready
def ready(_ctx):
    os.makedirs(WORKSPACE, exist_ok=True)
    return True  # warm: interpreter up, deps imported at module load


@app.on_validate
def validate(_ctx):
    # Exercise the hot path on a fresh VM so Lambda prefetches these
    # snapshot regions — measurably faster first /execute after launch.
    _run_code("import numpy, pandas; print(numpy.zeros(4).sum())", 20)


@app.on_run
def on_run(ctx):
    # Fresh identity per clone — anything build-time is shared by every VM.
    SESSION["id"] = ctx.get("microvmId") or str(uuid.uuid4())
    SESSION["executions"] = 0
```

`/ready` is the moment the snapshot is taken — the build only snapshots after it returns 200, so the interpreter is up and imports are done *before* the freeze. `/validate` runs on a restored clone at build time, and the pages it touches get prefetched on every future launch; running a real numpy execution there is free launch-latency insurance. `/run` fires on every clone and is where per-session identity is born — the snapshot is a photocopy, so anything unique (session IDs, RNG state, secrets) must be generated here, not at build time. The vendored `HookApp` already reseeds the RNG on `/run` for you.

The user-facing API is three routes. `/execute` runs code as a subprocess with a per-call `timeout` (default 30 s), captures stdout/stderr capped at 50 KB, and counts executions:

```python
@app.route("POST", "/execute")
def execute(body, _headers):
    code = body.get("code")
    if not code:
        return 400, {"error": "body must be {\"code\": \"...\"}"}
    try:
        result = _run_code(code, int(body.get("timeout", 30)))
    except subprocess.TimeoutExpired:
        return 408, {"error": "execution timed out"}
    SESSION["executions"] += 1
    result |= {"session": SESSION["id"], "execution_count": SESSION["executions"]}
    return 200, result
```

`/pip` shells out to `pip install` inside the VM (the agent asks for a library mid-session, it gets it), and `/state` reports session ID, execution count, and workspace contents. Deploy is three commands:

```console
$ mvm image build code-sandbox examples/code-sandbox
$ mvm run code-sandbox --wait
$ mvm call <id> /execute -X POST -d '{"code":"print(2+2)"}'
```

## Run it

Here's the live transcript against the deployed service:

![code-sandbox live demo](../benchmarks/results/demo-code-sandbox.svg)

`mvm run code-sandbox --wait` had the VM running and serving in **4.8 s** on this particular launch (across our 5-sample benchmark: p50 **3.54 s**, p95 **4.49 s**, best **3.46 s** to first authenticated byte). The first `/execute` — untrusted numpy eigenvalue code — completes in **1,197.5 ms** including the subprocess spawn on a cold page cache. The second call writes `model.bin` to the workspace in **10.4 ms** of in-VM time; end-to-end warm request latency over 20 samples is p50 **111.0 ms**, p95 **122.9 ms**, and that includes TLS, proxy auth, and the Python subprocess. `GET /state` confirms the contract: same session ID (`microvm-3467529c-…`), `executions: 2`, `model.bin` in the workspace, `pid: 1`. Then we terminate — the session and everything the untrusted code did vanish with the VM.

Note what happened during the run: the first request to a fresh VM costs ~700 ms (token mint included), then everything is fast. And if a session goes quiet, you don't have to choose between paying and killing it — the first request to a suspended VM auto-resumes it and returned **200 in 0.7 s** in our tests.

## What it costs

Rates in `us-east-1`: $0.0000276944/vCPU-s, $0.0000036667/GB-s, snapshot write $0.0038/GB and read $0.00155/GB, suspended storage $0.08/GB-month, per-second billing. For our 2 GB / 1 vCPU sandbox with its 0.61 GB snapshot:

| Session shape | MicroVM cost | Always-on 2 GB container | Savings |
|---|---|---|---|
| 8 s one-shot (terminate) | **$0.0003** | — | run 3,000+ per dollar |
| 30 min active + 8 h suspended | **$0.0669** | $1.0719 | **93.8%** |
| 2 h active + 22 h suspended | **$0.2602** | $3.0264 | **91.4%** |

One shape-specific decision matters here: **terminate one-shots, suspend conversations**. A suspend/resume cycle on our 0.61 GB snapshot costs ≈$0.0034 in snapshot I/O — more than ten times the entire compute cost of an 8-second job. If the agent asked one question and got one answer, terminate. Suspend pays for itself only when the session will plausibly continue and rebuilding its state (files, pip installs) would cost more than a third of a cent.

## The gotchas

**Nothing secret, nothing unique, in the image.** The snapshot clones every byte of build-time memory into every session's VM. An API key in an image env var is readable by every tenant's untrusted code; a session ID minted at build time is *the same* in all clones. Secrets come through the VM's execution role at runtime; identity comes from `/run`.

**Cap execution twice.** The per-call `timeout` (30 s default, 408 on expiry) stops a single runaway cell, but the VM itself needs a lifetime cap: idle detection keys off endpoint traffic, `suspendedDurationSeconds` doubles as an auto-terminate timer, and total lifetime is hard-capped at 8 h (28,800 s). Set these at `mvm run` time so an abandoned agent session can't bill for a day.

**ARM64 only.** The service is Graviton-only, so `/pip` installs must find aarch64 wheels. numpy, pandas, and the mainstream scientific stack are fine; niche packages with x86-only binary wheels will fall back to source builds inside the VM or fail. Bake the heavy, common ones into the image and treat `/pip` as the escape hatch.

**Don't push data through the endpoint.** Endpoint bandwidth is capped by VM size (1 MB/s at 0.5 GB up to 16 MB/s at 8 GB). Code snippets and JSON results are fine; if the sandbox needs a real dataset, hand it an S3 path and let the execution role fetch it.

## Take it further

- **Close the loop with a model** — [examples/ai-code-runner](../examples/ai-code-runner) has Bedrock generate code, run it in this sandbox, and feed tracebacks back for self-repair, with no keys in the image.
- **Pre-warm a pool** — `Fleet.scale_to(6)` took a 512 MiB variant from 0 to 6 running VMs in 9.7 s; hand a running VM to each new session for effectively zero start latency.
- **Lock down egress** — swap `INTERNET_EGRESS` for a VPC connector when the code being executed shouldn't be allowed to call home.

---

The sandbox app and transcript: [awesome-microvm](https://github.com/vivekrajaps/awesome-microvm) · the plane it runs on: [microvm-ctl](https://github.com/vivekrajaps/microvm-ctl) (`pip install microvm-ctl`) · Series: [00 — control & scale](00-control-and-scale-microvms-like-a-pro.md) · **01 — code sandbox** (this post)
