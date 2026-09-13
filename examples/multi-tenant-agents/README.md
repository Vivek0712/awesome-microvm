# Multi-tenant AI agents

One microVM per tenant. Identity arrives via runHookPayload at run time, so the image is tenant-agnostic. Conversation memory lives in VM RAM and survives suspend.

```console
mvm image build multi-tenant-agents examples/multi-tenant-agents
mvm run multi-tenant-agents --payload '{"tenant_id":"acme","display_name":"Acme Corp"}' --wait
mvm call <id> /whoami
mvm call <id> /chat -X POST -d '{"message":"hello"}'
```

Article: [Multi-tenant AI agents with one AWS Lambda MicroVM per tenant](../../blog/08-multi-tenant-agents.md). Live transcript: [demo-multi-tenant-agents.png](../../blog/img/demo-multi-tenant-agents.png).
