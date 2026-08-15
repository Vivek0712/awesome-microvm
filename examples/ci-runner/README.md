# Ephemeral CI runner

Clone → lint → test → report → terminate. Pattern C: no suspend, per-second billing, a poisoned dependency chain dies with the VM.

```console
mvm image build ci-runner examples/ci-runner
mvm run ci-runner --wait --max-duration 900
mvm call <id> /job -X POST -d '{"repo_url":"https://github.com/you/repo","steps":["pytest -q"]}'
mvm terminate <id>
```

Deep dive: [blog post](../../blog/06-ci-runner.md) · live transcript: [screenshot](../../benchmarks/results/demo-ci-runner.svg)
