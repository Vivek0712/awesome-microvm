# Code execution sandbox

Run untrusted or AI-generated Python inside a Firecracker microVM. Session state (files, pip installs) persists across calls and across suspend and resume.

```console
mvm image build code-sandbox examples/code-sandbox
mvm run code-sandbox --wait
mvm call <id> /execute -X POST -d '{"code":"print(2+2)"}'
mvm call <id> /pip -X POST -d '{"packages":["httpx"]}'
mvm call <id> /state
```

Series: [part 2, section 1 of Building on AWS Lambda MicroVMs](../../blog/01-seven-workloads.md). Full write-up: [Build a code execution sandbox on AWS Lambda MicroVMs](../../blog/deep-dives/01-code-sandbox.md). Code: [github.com/Vivek0712/awesome-microvm/tree/main/examples/code-sandbox](https://github.com/Vivek0712/awesome-microvm/tree/main/examples/code-sandbox). Live transcript: [demo-code-sandbox.png](../../blog/img/demo-code-sandbox.png).
