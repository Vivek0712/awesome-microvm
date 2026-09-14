# Agent evaluation fleet

One pristine, byte-identical environment per eval task. The harness scales a fleet, round-robins tasks over it, prints a scoreboard, and drains. The third task in tasks.json fails on purpose so the harness is seen to report failure.

```console
mvm image build agent-eval examples/agent-eval
python3 examples/agent-eval/harness.py --image agent-eval --workers 3 --tasks examples/agent-eval/tasks.json
```

Series: [part 2, section 3 of Building on AWS Lambda MicroVMs](https://builder.aws.com/content/3JJ2oNWY9EsZzivMMx044cSlrFQ/seven-workloads-lambda-could-never-run-until-microvms). Full write-up: [Evaluate agents on a fleet of identical AWS Lambda MicroVMs](../../blog/deep-dives/03-agent-eval.md). Code: [github.com/Vivek0712/awesome-microvm/tree/main/examples/agent-eval](https://github.com/Vivek0712/awesome-microvm/tree/main/examples/agent-eval). Live transcript: [demo-agent-eval.png](../../blog/img/demo-agent-eval.png).
