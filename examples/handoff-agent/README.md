# Handoff agent

The one image every orchestrator leases. The task is a list of shell steps; the lease (who to call back, and with what token) rides in `runHookPayload`, so the same image serves Step Functions, a Lambda durable function, an SQS or EventBridge poller, an HTTP collector, or nobody at all (`--kind none`, watch it through `/status`). Each step reports a phase, progress, and its output tail; the first failing step ends the lease with a typed `StepFailed`; the result carries `microvm_id` and the heartbeat count.

```console
mvm image build handoff-agent examples/handoff-agent
mvm lease run handoff-agent --kind none --task '{"steps":["echo hi","python3 -c \"print(2+2)\""]}' --wait
mvm watch <id>                # live phase, progress, heartbeats, and the streamed log
mvm status <id>               # one snapshot: lease.done, the result or the typed error in the log
mvm terminate <id>
mvm lease run handoff-agent --shards 4 --task-template '{"steps":["echo shard {i}","sleep 5"]}'
mvm watch --image handoff-agent   # one row per running member: lease id, phase, progress, elapsed; done D/N in the footer
```

Task shape: `{"steps": ["<shell>", ...], "workdir": "/tmp/job", "env": {"K": "V"}, "parallel": false, "max_parallel": 4}`. Result: `{"passed": true, "steps": [{"cmd", "exit_code", "duration_s", "output_tail"}], "parallel", "microvm_id", "heartbeats"}`. Steps run one after another unless `"parallel": true`, which runs them on a thread pool of `max_parallel` workers (default: the VM's CPU count, or 4) inside the one VM: progress counts completions, the phase reads `parallel 3/4 done`, results keep step order, and every failing step is reported in one `StepFailed` after all of them finish (four `sleep 1` steps take about 1 s in parallel and 4 s in sequence, as `/status` shows). Parallel steps are the in-VM lever; more VMs is the orchestrator's (`--shards`). Two test aids exercise the orchestrator side: `"fail_after_s": 5` sleeps then fails with a retryable `Injected` error (does the orchestrator relaunch?), and `"hang_s": 900` sleeps past any budget (does the heartbeat timeout fire and the VM get terminated?).

Orchestrators that lease this image: [stepfunctions-handoff](../stepfunctions-handoff) (a state machine with `waitForTaskToken`), [durable-handoff](../durable-handoff) (a Lambda durable function; that example ships its own security-review agent on the same runtime), and [generic-handoff](../generic-handoff) (a laptop controller over SQS, EventBridge, or an HTTP collector). The lease contract itself is documented in [microvm-ctl docs/integrations.md](https://github.com/Vivek0712/microvm-ctl/blob/main/docs/integrations.md).
