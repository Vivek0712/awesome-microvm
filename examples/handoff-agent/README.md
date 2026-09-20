# Handoff agent

The one image every orchestrator leases. The task is a list of shell steps; the lease (who to call back, and with what token) rides in `runHookPayload`, so the same image serves Step Functions, a Lambda durable function, an SQS or EventBridge poller, an HTTP collector, or nobody at all (`--kind none`, watch it through `/status`). Each step reports a phase, progress, and its output tail; the first failing step ends the lease with a typed `StepFailed`; the result carries `microvm_id` and the heartbeat count.

```console
mvm image build handoff-agent examples/handoff-agent
mvm lease run handoff-agent --kind none --task '{"steps":["echo hi","python3 -c \"print(2+2)\""]}' --wait
mvm watch <id>                # live phase, progress, heartbeats, and the streamed log
mvm status <id>               # one snapshot: lease.done, the result or the typed error in the log
mvm terminate <id>
```

Task shape: `{"steps": ["<shell>", ...], "workdir": "/tmp/job", "env": {"K": "V"}}`. Result: `{"passed": true, "steps": [{"cmd", "exit_code", "duration_s", "output_tail"}], "microvm_id", "heartbeats"}`. Two test aids exercise the orchestrator side: `"fail_after_s": 5` sleeps then fails with a retryable `Injected` error (does the orchestrator relaunch?), and `"hang_s": 900` sleeps past any budget (does the heartbeat timeout fire and the VM get terminated?).

Orchestrators that lease this image: [stepfunctions-handoff](../stepfunctions-handoff) (a state machine with `waitForTaskToken`), [durable-handoff](../durable-handoff) (a Lambda durable function; that example ships its own security-review agent on the same runtime), and [generic-handoff](../generic-handoff) (a laptop controller over SQS, EventBridge, or an HTTP collector). The lease contract itself is documented in [microvm-ctl docs/integrations.md](https://github.com/Vivek0712/microvm-ctl/blob/main/docs/integrations.md).
