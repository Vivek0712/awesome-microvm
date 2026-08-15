# Stateful notebook kernel

A Python namespace that lives in VM memory: variables survive across requests and suspend/resume — same process, same PID.

```console
mvm image build notebook examples/notebook
mvm run notebook --wait
mvm call <id> /cell -X POST -d '{"code":"x = 41"}'
mvm suspend <id>   # ...come back later...
mvm call <id> /cell -X POST -d '{"code":"x + 1"}'   # auto-resume; -> 42
```

Deep dive: [blog post](../../blog/04-notebook.md) · live transcript: [screenshot](../../benchmarks/results/demo-notebook.svg)
