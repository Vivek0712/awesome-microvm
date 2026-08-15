# Code execution sandbox

Run untrusted / AI-generated Python inside a Firecracker microVM. Session state (files, pip installs) persists across calls and suspend/resume.

```console
mvm image build code-sandbox examples/code-sandbox
mvm run code-sandbox --wait
mvm call <id> /execute -X POST -d '{"code":"print(2+2)"}'
mvm call <id> /pip -X POST -d '{"packages":["httpx"]}'
mvm call <id> /state
```

Deep dive: [blog post](../../blog/01-code-sandbox.md) · live transcript: [screenshot](../../benchmarks/results/demo-code-sandbox.svg)
