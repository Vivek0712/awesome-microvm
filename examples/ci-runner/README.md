# Ephemeral CI runner

Clone, lint, test, report, terminate. No suspend, per-second billing, and a poisoned dependency chain dies with the VM.

```console
mvm image build ci-runner examples/ci-runner
mvm run ci-runner --wait --max-duration 900
mvm call <id> /job -X POST -d '{"repo_url":"https://github.com/you/repo","steps":["pytest -q"]}'
mvm terminate <id>
```

Article: [Ephemeral CI runners on AWS Lambda MicroVMs: a fresh VM for every job](../../blog/06-ci-runner.md). Live transcript: [demo-ci-runner.png](../../blog/img/demo-ci-runner.png).
