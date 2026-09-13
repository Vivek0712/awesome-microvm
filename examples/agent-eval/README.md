# Agent evaluation fleet

One pristine, byte-identical environment per eval task. The harness scales a fleet, round-robins tasks over it, prints a scoreboard, and drains. The third task in tasks.json fails on purpose so the harness is seen to report failure.

```console
mvm image build agent-eval examples/agent-eval
python3 examples/agent-eval/harness.py --image agent-eval --workers 3 --tasks examples/agent-eval/tasks.json
```

Article: [Evaluate agents on a fleet of identical AWS Lambda MicroVMs](../../blog/03-agent-eval.md). Live transcript: [demo-agent-eval.png](../../blog/img/demo-agent-eval.png).
