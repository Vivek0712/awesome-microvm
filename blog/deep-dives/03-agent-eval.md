---
title: "Evaluate agents on a fleet of identical AWS Lambda MicroVMs"
description: "Fan an eval suite across pristine Firecracker clones: 0 to 6 running VMs in 9.7 seconds, every score from an environment that is byte-for-byte identical to every other, and the whole fleet drained in 0.7 seconds when the scoreboard prints."
---

Contamination ruins eval pipelines quietly. Task 47 installs a package, task 48 inherits it and passes tests it should have failed. A previous run leaves a file in /tmp and your pass rate drifts by two points between Tuesday and Thursday. Labs that take agent evaluation seriously insist on one disposable, identical environment per task. AWS Lambda MicroVMs gives you that primitive natively: every VM you launch from an image is a restored copy of the same memory-and-disk snapshot. That is stronger than "same Dockerfile, rebuilt"; it is the same bytes.

This article walks through examples/agent-eval in the awesome-microvm repo: a standard-library eval worker, a harness that fans a task suite across a fleet with Fleet.scale_to, and a deliberately broken third task to prove the harness reports failure honestly. Everything below was measured on the live service in us-east-1. This is part 4 of the series Building on AWS Lambda MicroVMs.

## Why a microVM and not a container or a Lambda function

Containers give you filesystem isolation but not determinism. Two containers from one image diverge the moment PID 1 starts: different entropy, different timing, different package resolution if anything installs at runtime. A warm container pool is worse, because reuse is exactly the contamination you are trying to kill. Lambda functions isolate well but cap execution time and give you no way to run an arbitrary long-lived pytest process against a workspace you control.

A microVM clone restores the entire machine state from a snapshot: every process's memory, the full disk, even RNG state. For eval, that means the interpreter, the installed pytest, and every byte of the filesystem are provably identical across workers and across runs. The failure mode changes from silent drift to a known, documented issue, snapshot uniqueness, which the runtime hooks exist to solve. And because launch-to-serving measured at p50 3.54 s, one fresh VM per shard is something you do casually rather than as a provisioning project.

## Architecture

![Agent eval architecture: the harness scales a fleet through the quota-aware FleetManager and round-robins /evaluate calls over per-VM clients, every VM restored from one snapshot](../img/arch-03-agent-eval.png)

The control plane builds the image once, launches N clones through a token-bucket throttle read from your applied Service Quotas, and terminates them at the end. The execution plane mints a port-scoped JWE auth token per VM (there is no load balancer; each VM gets its own HTTPS endpoint) and round-robins tasks over the fleet. Every worker restores from the same 602 MB memory / 24 MB disk snapshot, built once in 133.3 s.

## Build it

The Dockerfile says what the design relies on:

```dockerfile
# Every VM is a byte-for-byte clone of the same snapshot: no contamination
# between runs, no flaky shared state, horizontal fan-out limited only by quota.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y python3.12 python3.12-pip git && dnf clean all
RUN python3.12 -m pip install --no-cache-dir pytest requests

WORKDIR /app
COPY microvm_hooks.py app.py /app/

EXPOSE 8080
ENTRYPOINT ["python3.12", "/app/app.py"]
```

The worker is a zero-dependency HTTP server on the vendored HookApp. Two hooks matter for this use case. /ready gates the snapshot: the build only captures state after it returns 200, so pytest is imported and warm in every clone. /run fires on each launched clone and is where per-VM identity enters a world of identical bytes:

```python
@app.on_run
def on_run(ctx):
    IDENTITY["worker"] = str(uuid.uuid4())[:8]
    payload = ctx.get("runHookPayload")
    IDENTITY["assignment"] = json.loads(payload) if payload else None  # e.g. {"shard": 3}
```

This is the snapshot-uniqueness fix in miniature. The UUID is generated after restore (HookApp also reseeds the RNG in /run; otherwise every clone would draw the same "random" numbers), and runHookPayload is the per-VM channel for shard assignments. Image environment variables are the wrong place for that, because they are image-level and shared by every clone.

The /evaluate route wipes its workspace before every task, so even task reuse within one worker starts clean:

```python
@app.route("POST", "/evaluate")
def evaluate(body, _headers):
    for f in os.listdir(WORK):  # each task starts from a clean slate
        path = os.path.join(WORK, f)
        subprocess.run(["rm", "-rf", path])
    ...
    proc = _sh(["python3.12", "-m", "pytest", "test_candidate.py", "-q", "--tb=line"])
```

The harness is where the fleet mechanics live. Scale-out is one call, and the hard lifetime cap is there because eval fleets are disposable by construction:

```python
fleet = Fleet(
    FleetManager(cfg), args.image,
    idle_policy=IdlePolicy(max_idle=600, suspended_for=60, auto_resume=False),
    max_duration=3600,  # eval fleets are disposable; hard cap their lifetime
)
fleet.scale_to(args.workers, wait_running=True)
```

Routing is plain round-robin over per-VM endpoint clients, fanned out on a thread pool:

```python
def run_task(i_task):
    i, task = i_task
    client = clients[i % len(clients)]
    resp = client.post("/evaluate", json=task, timeout=180)
    return task["task_id"], resp.json() if resp.ok else {"passed": False, "error": resp.text}

with futures.ThreadPoolExecutor(max_workers=len(clients)) as pool:
    results = dict(pool.map(run_task, enumerate(tasks)))
```

When the scoreboard prints, the fleet dies: fleet.drain() terminates every member unless you passed --keep-fleet.

The task suite includes a canary. tasks.json ships three tasks, and the third is wrong on purpose:

```json
{
  "task_id": "broken-on-purpose",
  "candidate_code": "def add(a, b):\n    return a - b\n",
  "tests": "from candidate import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"
}
```

An eval harness that has never been seen to fail is an eval harness you cannot trust. This one must report 2 of 3 or it is broken.

## Run it

The demo transcript runs the suite against a single worker:

![Agent eval live demo: launch, three tasks, two pass and the canary fails, terminate](../img/demo-agent-eval.png)

`mvm run agent-eval --wait` has the VM RUNNING and serving authenticated traffic in 3.5 s. Then the scoreboard, exactly as the tasks predict: PASS fibonacci (2 passed in 0.01 s), PASS slugify (2 passed in 0.00 s), FAIL broken-on-purpose (1 failed in 0.00 s). The canary is caught, the score is 2 of 3, and the VM is terminated.

The fleet path is where the numbers matter. On this account, scale_to(6) took a fresh fleet from 0 to 6 RUNNING microVMs in 9.7 s wall, with FleetManager throttling RunMicrovm to 0.8 per second, 80% of the applied 1 per second quota. That run used the 512 MiB sandbox-small image; the quota section below explains why. drain() terminated all 6 in 0.7 s. Per-task overhead once workers are up is small: a warm authenticated request into a VM lands at p50 111 ms, and each /evaluate is that plus your pytest runtime.

## What it costs

Rates in us-east-1, billed per second:

| Item | Rate |
|---|---|
| vCPU | $0.0000276944 per vCPU-second |
| Memory | $0.0000036667 per GB-second |
| Snapshot write / read | $0.0038 / $0.00155 per GB |
| Suspended and image storage | $0.08 per GB-month |

Measured with the repo's cost model on a 2 GB / 1 vCPU VM with my 0.61 GB snapshot: an 8 second one-shot job that terminates costs $0.0003. A whole 6-worker eval run is six of those plus your test runtime, which is fractions of a cent per suite.

The part specific to eval fleets is to terminate rather than suspend. Suspend and resume is the service's headline feature, and for a notebook or a chat agent it is the right call. A 30 minute active plus 8 hour suspended session came out 93.8% cheaper than always-on. But one suspend and resume cycle on a 0.61 GB snapshot costs about $0.0033 in snapshot write plus read, more than ten times the entire $0.0003 one-shot run. An eval worker has no state worth $0.0033 to preserve. Its whole value is that the next run starts from the pristine image snapshot rather than from whatever the last task did to the filesystem. Suspending an eval worker pays extra to keep exactly the contamination you built this to avoid. An always-on 2 GB runner, for comparison, is about $3.03 per day whether or not any evals run.

## The gotchas

- The applied memory quota is your real fleet ceiling. My fresh account's applied quota was 8 GB of total microVM memory, against a published default of 1,024 GB, and it counts RUNNING, SUSPENDED, TERMINATING, and image-build VMs. With 2 GB eval workers that is a 4-VM fleet, minus headroom for VMs still tearing down. I hit ServiceQuotaExceededException twice: once when five concurrent image builds ate the quota, once when freshly terminated VMs still counted. Fixes, in order: shrink the worker (my 6-VM fan-out ran the 512 MiB image), let FleetManager read applied quotas at startup and throttle to 80% (it does), and file the raise on day one. My RunMicrovm request from 1 to 5 per second was filed with a single API call; the case closed with the applied value unchanged, so plan for that outcome too.
- Clones share everything from the snapshot, including entropy. Without the /run-time RNG reseed and UUID generation shown above, "random" worker IDs and any stochastic test behavior would be identical across the fleet. Never bake per-worker values or secrets into the image; use runHookPayload and the execution role.
- One endpoint per VM, no load balancer. The harness holds N EndpointClients and does its own round-robin. Each client mints and caches its own port-scoped token. That first token mint makes the first request to a fresh VM about 700 ms before settling to about 111 ms.
- ARM64 only. The workers are Graviton. Pure-Python eval suites do not care. Audit wheels before pointing this at a suite with native dependencies.

## Take it further

- Shard real suites via runHookPayload. Pass {"shard": k} at launch and have each worker pull its slice from S3 instead of round-robinning tasks over HTTP. The endpoint is bandwidth-capped at 1 to 16 MB/s, so bulk data belongs on S3 or EFS.
- Score model-generated code by pointing the AI code runner from part 3 at this harness: generate on one fleet, evaluate on another, no shared state anywhere.
- Exercise the hot path in /validate. It runs on a restored clone and the service prefetches the snapshot pages it touches, so a dry-run pytest there shaves the first real task's cold path.

The eval worker, harness, and tasks are in [awesome-microvm](https://github.com/Vivek0712/awesome-microvm) under examples/agent-eval. The plane and CLI behind every number are [microvm-ctl](https://github.com/Vivek0712/microvm-ctl). This is part 4 of Building on AWS Lambda MicroVMs. Part 5 keeps state alive instead of throwing it away: a notebook kernel that suspends for free.
