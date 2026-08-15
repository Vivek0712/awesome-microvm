# awesome-microvm

**Everything we've built, measured, and written about [AWS Lambda MicroVMs](https://docs.aws.amazon.com/lambda/latest/dg/lambda-microvms-guide.html)** — eight production-shaped example apps, a nine-post blog series, and live-service benchmark transcripts. All of it runs on **[microvm-ctl](https://github.com/vivekrajaps/microvm-ctl)**, our open-source control & execution plane for the service (`pip install microvm-ctl`).

Lambda MicroVMs hands you a Firecracker VM with a controllable lifecycle — run, suspend (billing stops, state frozen), resume (same PID, every byte intact), terminate — and a dedicated authenticated endpoint. What you build on that primitive is the interesting part. This repo is the gallery.

## Get started

```
pip install microvm-ctl        # until the first PyPI release:
                               #   pip install git+https://github.com/vivekrajaps/microvm-ctl
mvm bootstrap                  # one-time: S3 artifact bucket + IAM roles
mvm image build code-sandbox examples/code-sandbox
mvm run code-sandbox --wait
mvm call <microvm-id> /execute -X POST -d '{"code":"print(2+2)"}'
```

## The use cases

Each example is a Dockerfile + a single-file app on the plane's zero-dependency hook runtime, deployed and exercised on the live service — every screenshot below is a real transcript.

| Example | Pattern | What it shows | Blog |
|---|---|---|---|
| [`code-sandbox`](examples/code-sandbox) | sandbox | Untrusted/AI Python; state persists across calls | [post](blog/01-code-sandbox.md) |
| [`ai-code-runner`](examples/ai-code-runner) | agent-in-VM | Bedrock writes code → VM runs it → self-repair loop | [post](blog/02-ai-code-runner.md) |
| [`agent-eval`](examples/agent-eval) | fan-out | N pristine clones, one eval task each, scoreboard | [post](blog/03-agent-eval.md) |
| [`notebook`](examples/notebook) | stateful session | Variables survive suspend/resume (same PID) | [post](blog/04-notebook.md) |
| [`data-analytics`](examples/data-analytics) | large working set | Sandboxed DuckDB over S3; bulk data off the endpoint | [post](blog/05-data-analytics.md) |
| [`ci-runner`](examples/ci-runner) | ephemeral job | Clone → test → terminate; per-second billing | [post](blog/06-ci-runner.md) |
| [`pdf-service`](examples/pdf-service) | bursty service | Untrusted HTML→PDF; sleeps between bursts | [post](blog/07-pdf-service.md) |
| [`multi-tenant-agents`](examples/multi-tenant-agents) | VM-per-tenant | Tenant identity via `runHookPayload`, near-zero idle cost | [post](blog/08-multi-tenant-agents.md) |

## The blog series

Start with the flagship — **[Control and scale AWS Lambda MicroVMs like a pro](blog/00-control-and-scale-microvms-like-a-pro.md)** — then the eight use-case deep-dives linked above. House rule for the whole series: every number is measured on the live service, every gotcha is one we actually hit.

Headline measurements (us-east-1, reproducible with the plane's [benchmark harness](https://github.com/vivekrajaps/microvm-ctl/blob/main/benchmarks/benchmark.py)):

| What | Measured |
|---|---|
| Image build (Dockerfile → runnable snapshot) | 123–145 s |
| `RunMicrovm` → serving authenticated traffic | **p50 3.54 s**, p95 4.49 s |
| Warm authenticated request (real Python exec in VM) | **p50 111 ms** |
| Suspend / resume | 2.5 s / 2.6 s — same PID, state intact |
| Auto-resume (first request to a suspended VM) | **200 OK in 0.7 s** |
| Fleet scale-out 0 → 6 running VMs | **9.7 s wall** |
| 30 min active + 8 h suspended session | **93.8% cheaper** than always-on |

Raw results + SVG terminal transcripts: [`benchmarks/results/`](benchmarks/results/). The demo recorder that produced them: [`benchmarks/capture_demos.py`](benchmarks/capture_demos.py).

## Repo layout

```
examples/       8 runnable use cases (Dockerfile + single-file app each)
blog/           the 9-post series
benchmarks/     capture_demos.py + recorded results (JSON, SVG transcripts, a rendered PDF)
```

The plane itself — SDK, `mvm` CLI, quota-aware fleet manager, endpoint client, hook runtime, docs — lives in [microvm-ctl](https://github.com/vivekrajaps/microvm-ctl) (Apache-2.0).

## License

MIT (examples and content). The plane is Apache-2.0 in its own repo.
