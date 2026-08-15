# Multi-tenant AI agents

One microVM per tenant; identity arrives via runHookPayload at run time — the image is tenant-agnostic. Conversation memory lives in VM RAM and survives suspend.

```console
mvm image build multi-tenant-agents examples/multi-tenant-agents
mvm run multi-tenant-agents --payload '{"tenant_id":"acme","display_name":"Acme Corp"}' --wait
mvm call <id> /chat -X POST -d '{"message":"hello"}'
```

Deep dive: [blog post](../../blog/08-multi-tenant-agents.md) · live transcript: [screenshot](../../benchmarks/results/demo-multi-tenant-agents.svg)
