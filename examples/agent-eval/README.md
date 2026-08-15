# Agent evaluation fleet

One pristine, byte-identical environment per eval task. The harness scales a fleet, round-robins tasks, prints a scoreboard, drains.

```console
mvm image build agent-eval examples/agent-eval
python3 examples/agent-eval/harness.py --image agent-eval --workers 3 --tasks examples/agent-eval/tasks.json
```

Deep dive: [blog post](../../blog/03-agent-eval.md) · live transcript: [screenshot](../../benchmarks/results/demo-agent-eval.svg)
