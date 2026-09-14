# Stateful notebook kernel

A Python namespace that lives in VM memory. Variables survive across requests and across suspend and resume, in the same process with the same PID.

```console
mvm image build notebook examples/notebook
mvm run notebook --wait --idle 900 --suspended-ttl 28800
mvm call <id> /cell -X POST -d '{"code":"x = 41"}'
mvm suspend <id>                                   # come back later
mvm call <id> /cell -X POST -d '{"code":"x + 1"}'  # auto-resumes; returns 42
```

Series: [part 2, section 4 of Building on AWS Lambda MicroVMs](../../blog/01-seven-workloads.md). Full write-up: [A stateful notebook kernel that suspends for free on AWS Lambda MicroVMs](../../blog/deep-dives/04-notebook.md). Code: [github.com/Vivek0712/awesome-microvm/tree/main/examples/notebook](https://github.com/Vivek0712/awesome-microvm/tree/main/examples/notebook). Live transcript: [demo-notebook.png](../../blog/img/demo-notebook.png).
