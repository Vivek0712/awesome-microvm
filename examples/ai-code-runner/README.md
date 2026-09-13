# AI code runner

Bedrock writes the code, the microVM runs it, and tracebacks feed back until the script exits cleanly. The self-repair loop lives inside the VM. Credentials come from the execution role, never the image.

```console
mvm image build ai-code-runner examples/ai-code-runner
mvm run ai-code-runner --wait
mvm call <id> /solve -X POST -d '{"task":"plot a sine wave to sine.png"}'
```

Article: [Run model-written code safely: an AI code runner on AWS Lambda MicroVMs](../../blog/02-ai-code-runner.md). Live transcript: [demo-ai-code-runner.png](../../blog/img/demo-ai-code-runner.png).
