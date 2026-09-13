# Stateful notebook kernel

A Python namespace that lives in VM memory. Variables survive across requests and across suspend and resume, in the same process with the same PID.

```console
mvm image build notebook examples/notebook
mvm run notebook --wait --idle 900 --suspended-ttl 28800
mvm call <id> /cell -X POST -d '{"code":"x = 41"}'
mvm suspend <id>                                   # come back later
mvm call <id> /cell -X POST -d '{"code":"x + 1"}'  # auto-resumes; returns 42
```

Article: [A stateful notebook kernel that suspends for free on AWS Lambda MicroVMs](../../blog/04-notebook.md). Live transcript: [demo-notebook.png](../../blog/img/demo-notebook.png).
