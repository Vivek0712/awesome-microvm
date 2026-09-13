# Code execution sandbox

Run untrusted or AI-generated Python inside a Firecracker microVM. Session state (files, pip installs) persists across calls and across suspend and resume.

```console
mvm image build code-sandbox examples/code-sandbox
mvm run code-sandbox --wait
mvm call <id> /execute -X POST -d '{"code":"print(2+2)"}'
mvm call <id> /pip -X POST -d '{"packages":["httpx"]}'
mvm call <id> /state
```

Article: [Build a code execution sandbox on AWS Lambda MicroVMs](../../blog/01-code-sandbox.md). Live transcript: [demo-code-sandbox.png](../../blog/img/demo-code-sandbox.png).
