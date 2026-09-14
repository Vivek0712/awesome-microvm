# AI code runner

Bedrock writes the code, the microVM runs it, and tracebacks feed back until the script exits cleanly. The self-repair loop lives inside the VM. Credentials come from the execution role, never the image.

```console
mvm image build ai-code-runner examples/ai-code-runner
mvm run ai-code-runner --wait
mvm call <id> /solve -X POST -d '{"task":"plot a sine wave to sine.png"}'
```

Series: [part 2, section 2 of Building on AWS Lambda MicroVMs](https://builder.aws.com/content/3JJ2oNWY9EsZzivMMx044cSlrFQ/seven-workloads-lambda-could-never-run-until-microvms). Full write-up: [Run model-written code safely: an AI code runner on AWS Lambda MicroVMs](../../blog/deep-dives/02-ai-code-runner.md). Code: [github.com/Vivek0712/awesome-microvm/tree/main/examples/ai-code-runner](https://github.com/Vivek0712/awesome-microvm/tree/main/examples/ai-code-runner). Live transcript: [demo-ai-code-runner.png](../../blog/img/demo-ai-code-runner.png).
