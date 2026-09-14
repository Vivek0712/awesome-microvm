# Ephemeral CI runner

Clone, lint, test, report, terminate. No suspend, per-second billing, and a poisoned dependency chain dies with the VM.

```console
mvm image build ci-runner examples/ci-runner
mvm run ci-runner --wait --max-duration 900
mvm call <id> /job -X POST -d '{"repo_url":"https://github.com/you/repo","steps":["pytest -q"]}'
mvm terminate <id>
```

Series: [part 2, section 6 of Building on AWS Lambda MicroVMs](https://builder.aws.com/content/3JJ2oNWY9EsZzivMMx044cSlrFQ/seven-workloads-lambda-could-never-run-until-microvms). Full write-up: [Ephemeral CI runners on AWS Lambda MicroVMs: a fresh VM for every job](../../blog/deep-dives/06-ci-runner.md). Code: [github.com/Vivek0712/awesome-microvm/tree/main/examples/ci-runner](https://github.com/Vivek0712/awesome-microvm/tree/main/examples/ci-runner). Live transcript: [demo-ci-runner.png](../../blog/img/demo-ci-runner.png).
