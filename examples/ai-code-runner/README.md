# AI code runner

Bedrock writes the code, the microVM runs it, tracebacks feed back until it works. The self-repair loop lives inside the VM; credentials come from the execution role (never the image).

```console
mvm image build ai-code-runner examples/ai-code-runner
mvm run ai-code-runner --wait
mvm call <id> /solve -X POST -d '{"task":"plot a sine wave to sine.png"}'
```

Deep dive: [blog post](../../blog/02-ai-code-runner.md) · live transcript: [screenshot](../../benchmarks/results/demo-ai-code-runner.svg)
