# Tenant isolation is a kernel, not a namespace

*One Lambda MicroVM per customer, each running a private Bedrock-backed assistant — identity injected at launch via `runHookPayload`, conversation history held in VM memory, and a 93.8% discount whenever the tenant walks away.*

Every ISV building an AI assistant hits the same fork: tenant Acme's conversation history, credentials, and prompts must never be reachable from tenant Globex's process — and the usual answer is a Kubernetes-shaped platform with namespaces, network policies, and a row-level-security prayer. In this post we take the blunt approach instead: one Firecracker microVM per tenant. Acme gets a kernel. Globex gets a different kernel. The isolation argument ends there, and the bill stays sane because a tenant who isn't talking costs snapshot storage only.

This is post 08 in the [awesome-microvm](https://github.com/vivekrajaps/awesome-microvm) series. Everything here was run against the live service in `us-east-1` in August 2026.

## Why a microVM (and not a container or a Lambda function)

- **A container per tenant** shares the host kernel with every other tenant. Namespaces and cgroups are a resource boundary, not a security boundary, and a multi-tenant AI product is precisely the workload where a hostile tenant gets to type inputs into your process all day. A Firecracker microVM gives each tenant the boundary AWS itself uses to separate Lambda customers.
- **A Lambda function per tenant** has the isolation but not the state. A conversation is stateful — history, a warm Bedrock client, whatever the tenant uploaded — and Lambda's short stateless invocations force all of that out to a database on every turn. A microVM holds it in RAM and suspends with it.
- **A dedicated container/EC2 per tenant** (Fargate task, small instance) has both, but bills 24/7. An always-on 2 GB VM shape costs about $3.03/day; most tenants are idle most of the day. MicroVMs launch to serving authenticated traffic in p50 3.54 s (p95 4.49 s), suspend in 2.5 s, and auto-resume on the tenant's next request in 0.7 s — so "off when idle" is invisible to the tenant.

One line of rationale for this use case: hard per-tenant isolation with per-second billing that rounds to zero when the tenant is quiet — without operating a scheduler, an ingress layer, or a node pool.

## Architecture

```mermaid
flowchart LR
    subgraph control["Control plane (mvm CLI / Fleet)"]
        IB["ImageBuilder\none tenant-agnostic image"]
        FM["Fleet + run_payload_factory\none RunMicrovm per tenant"]
        TK["CreateMicrovmAuthToken\nport-scoped JWE per VM"]
    end
    subgraph acme["microVM — tenant acme"]
        HA1["HookApp :8080\n/run gets runHookPayload"]
        H1["HISTORY (RAM)"]
    end
    subgraph globex["microVM — tenant globex"]
        HA2["HookApp :8080"]
        H2["HISTORY (RAM)"]
    end
    BR["Bedrock (nova-lite)\nvia execution role"]
    FM -->|"runHookPayload = {tenant_id...}"| HA1
    FM --> HA2
    TK -.->|X-aws-proxy-auth| HA1
    HA1 --> BR
    HA2 --> BR
```

There is exactly one image, and it knows nothing about any tenant. The control plane launches one VM per tenant and injects identity at run time through `runHookPayload`; each VM gets its own dedicated HTTPS endpoint (`<id>.lambda-microvm.us-east-1.on.aws` — there is no shared load balancer to misroute a request) and calls Bedrock through the VM's execution role. Tokens are port-scoped JWEs minted per VM, so an Acme token is useless against Globex's endpoint.

## Build it

The Dockerfile is deliberately boring — that is the point. Nothing tenant-specific may exist at build time, because the build produces a snapshot that every clone starts from:

```dockerfile
# Multi-tenant AI agents — one microVM per tenant, identity injected at run
# time via runHookPayload. The image is tenant-agnostic; suspended tenants
# cost snapshot storage only, so 1,000 mostly-idle tenants stay affordable.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y python3.12 python3.12-pip && dnf clean all
RUN python3.12 -m pip install --no-cache-dir boto3

WORKDIR /app
COPY microvm_hooks.py app.py /app/

EXPOSE 8080
ENTRYPOINT ["python3.12", "/app/app.py"]
```

Building it takes 123.2 s and yields a 604 MB memory snapshot plus 23 MB of disk.

Now the anti-pattern, because it is the first thing everyone reaches for: **do not put per-tenant values in image env vars.** Env vars on a MicroVM image are image-level — set once at build, capped at 50, shared by every clone launched from that image, and persisted into the snapshot. `TENANT_ID=acme` as an env var means every tenant is Acme, forever, and rotating it means rebuilding the image. The service gives you a per-*launch* channel instead: `runHookPayload`, a string handed to `RunMicrovm` and delivered to the VM's `/run` hook. That is where identity belongs:

```python
TENANT: dict = {}
HISTORY: list[dict] = []
_bedrock = None

@app.on_run
def on_run(ctx):
    global _bedrock
    payload = ctx.get("runHookPayload")
    TENANT.update(json.loads(payload) if payload else {"tenant_id": "unassigned"})
    import boto3
    _bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))
```

The same run-time rule covers credentials: there is no Bedrock key in the image. The Bedrock client picks up the execution role attached to `RunMicrovm`, so each tenant's VM can carry its own scoped role.

Conversation memory is just a module-level list. `/chat` appends the user turn, calls `converse` with the last 20 messages and a per-tenant system prompt, and appends the reply:

```python
HISTORY.append({"role": "user", "content": [{"text": msg}]})
resp = _bedrock.converse(
    modelId=MODEL,
    messages=HISTORY[-20:],
    system=[{"text": f"You are the private assistant of tenant "
                     f"{TENANT.get('display_name') or TENANT.get('tenant_id')}. Be concise."}],
    inferenceConfig={"maxTokens": 500},
)
```

No database. When the idle policy suspends the VM, the snapshot captures the memory of every process — `HISTORY` included. Our suspend/resume fidelity run measured PID 1 before suspend and PID 1 after resume (same process, in-memory state preserved), which is exactly why `/whoami` reports its `pid`: it is the tenant-visible proof that the conversation never left RAM. The one thing that does *not* survive the freeze is TCP: pre-suspend connections are stale, so the resume hook rebuilds the client:

```python
@app.on_resume
def on_resume(ctx):
    global _bedrock
    import boto3  # refresh clients; pre-suspend connections may be stale
    _bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))
```

Launching the fleet is one `Fleet` with a `run_payload_factory` — a callable that maps a member index to that VM's payload:

```python
tenants = [("acme", "Acme Corp"), ("globex", "Globex"), ("initech", "Initech")]

fleet = Fleet(
    manager=manager,                      # cfg carries execution_role_arn
    image="multi-tenant-agents",
    idle_policy=IdlePolicy(max_idle=300, suspended_for=7200),
    run_payload_factory=lambda i: json.dumps(
        {"tenant_id": tenants[i][0], "display_name": tenants[i][1]}
    ),
)
fleet.scale_to(len(tenants), wait_running=True)
```

The `IdlePolicy` does the operational heavy lifting. `max_idle=300` suspends any tenant quiet for five minutes (idle detection keys off endpoint traffic). `suspended_for=7200` maps to `suspendedDurationSeconds`, which doubles as an auto-terminate timer: a tenant who suspends and never comes back is *terminated by the service* two hours later. That is your churned-tenant garbage collector — no reaper cron required, though `fleet.reap()` exists as belt-and-braces.

## Run it

![Live transcript: launch, /whoami, /chat](../benchmarks/results/demo-multi-tenant-agents.svg)

The capture above is a real run. `mvm run multi-tenant-agents --wait` reached RUNNING and serving in 12.5 s for this VM. `GET /whoami` then returned:

```json
{"tenant": {"tenant_id": "acme", "display_name": "Acme Corp"},
 "conversation_turns": 0, "pid": 1}
```

That JSON is the whole thesis in one response: `acme` appears nowhere in the image — it arrived in `runHookPayload` on this launch, and a sibling VM launched seconds later from the identical snapshot would report a different tenant.

Then we ask the tenant's assistant why it gets its own VM. `POST /chat` comes back in 565 ms end-to-end through Bedrock:

```json
{"tenant": "acme",
 "reply": "We get our own VM to ensure security, privacy, and dedicated resources tailored specifically to Acme Corp's needs.",
 "turns": 1, "latency_ms": 565}
```

The system prompt that made nova-lite say "Acme Corp" was assembled from the payload, not the image, and the turn counter it reports is the in-RAM `HISTORY`. One aside worth knowing: an earlier capture of this same demo, launched *without* the execution role, failed instantly with `botocore.exceptions.NoCredentialsError` from inside the VM — which is exactly what you want. There is no baked-in key to fall back on; forget `executionRoleArn` on `RunMicrovm` and the failure is loud, not a silent credential shared across tenants.

## What it costs

| Rate (us-east-1) | |
|---|---|
| vCPU | $0.0000276944 /vCPU-s |
| Memory | $0.0000036667 /GB-s |
| Snapshot write / read | $0.0038 / $0.00155 per GB |
| Suspended + image storage | $0.08 /GB-month |

The tenant workload shape is "bursts of chat, long gaps". Our CostModel's worked example on a 2 GB / 1 vCPU VM with a 0.61 GB snapshot: **30 min active + 8 h suspended costs $0.0669, versus $1.0719 for the same VM always-on — 93.8% cheaper.** A heavier tenant (2 h active + 22 h suspended) still saves 91.4%.

Between sessions, a fully idle tenant is a suspended snapshot: 0.61 GB × $0.08/GB-month ≈ $0.05 per tenant per month. A thousand dormant tenants sit at roughly $49/month of storage — which is what "near-zero idle cost" means with the units attached. Two caveats keep it honest: each suspend/resume cycle costs about $0.0034 in snapshot I/O on this size, so don't set `max_idle` so aggressive that a chatty tenant cycles every minute; and the always-on shape ($3.03/day) is where microVMs lose to Fargate — if a tenant genuinely talks all day, give them a container.

The real tenant-count ceiling is not price, it's the **memory quota**. `Max allocated MicroVM memory` counts RUNNING *and* SUSPENDED (and TERMINATING, and image-build) VMs, and our fresh account's applied quota was 8 GB against a published default of 1024 GB — four 2 GB tenants, total, including the sleeping ones. Even the published default caps you at 512 tenants at 2 GB each. Treat quota headroom as a launch deliverable: our RunMicrovm raise from 1/s to 5/s was auto-approved from a single API call, and the memory raise is the same motion.

## The gotchas

- **Suspended tenants occupy quota.** The economics say "keep 1,000 tenants suspended"; the memory quota says those 1,000 count as allocated. Size the quota request for peak *allocated* tenants, not peak concurrent chatters — or let `suspendedDurationSeconds` terminate the long tail and re-launch on demand.
- **8-hour lifetime ceiling.** Total VM lifetime maxes out at 28,800 s, so a tenant VM is a session-scale object, not a permanent home. Durable tenancy is: checkpoint `HISTORY` to S3 in the `/suspend` or `/terminate` hook, and re-launch with the same `runHookPayload` plus a history pointer.
- **Onboarding is rate-limited.** At our fresh account's applied 1/s `RunMicrovm` quota, launching 1,000 tenant VMs is a ~17-minute serial exercise. FleetManager reads applied quotas at startup and throttles to 80% so the burst degrades gracefully instead of erroring.
- **The snapshot clones everything.** Anything with per-VM uniqueness — RNG state, generated IDs, open connections — is copied into every tenant's VM. The hook server reseeds RNG on `/run`; your job is to keep tenant data out of build time entirely, which is the env-var anti-pattern again wearing a different hat.

## Take it further

- **One execution role per tenant.** `manager.run(..., execution_role=...)` accepts a per-launch role, so Acme's VM can be IAM-scoped to Acme's S3 prefix and Bedrock guardrail — isolation at the credential layer to match the kernel layer.
- **Checkpoint history on suspend.** Add an `on_suspend` hook that writes `HISTORY` to S3 via the execution role; the VM becomes disposable and the 8-hour ceiling stops mattering.
- **Per-tenant model tiers.** Put `model_id` in the `runHookPayload` next to `tenant_id` and read it in `/run` — premium tenants get a bigger model from the same image, with zero rebuilds.

---

Code, benchmark harness, and all eight example images: [github.com/vivekrajaps/awesome-microvm](https://github.com/vivekrajaps/awesome-microvm)

*Series: [00 — Control and scale MicroVMs like a pro](00-control-and-scale-microvms-like-a-pro.md) · [02 — AI code runner](02-ai-code-runner.md) · [07 — PDF service](07-pdf-service.md) · **08 — Multi-tenant agents***
