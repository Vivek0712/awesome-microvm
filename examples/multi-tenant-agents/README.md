# Multi-tenant AI agents

One microVM per tenant. Identity arrives via runHookPayload at run time, so the image is tenant-agnostic. Conversation memory lives in VM RAM and survives suspend.

```console
mvm image build multi-tenant-agents examples/multi-tenant-agents
mvm run multi-tenant-agents --payload '{"tenant_id":"acme","display_name":"Acme Corp"}' --wait
mvm call <id> /whoami
mvm call <id> /chat -X POST -d '{"message":"hello"}'
```

Series: [part 3 of Building on AWS Lambda MicroVMs](https://builder.aws.com/content/3JJ7tASPSSUUTrcpWnWtxOuu8g3/a-kernel-for-every-customer-scaling-ai-agents-to-1000-tenants-on-aws-lambda-microvms-with-microvm-ctl). Full write-up: [Multi-tenant AI agents with one AWS Lambda MicroVM per tenant](../../blog/deep-dives/08-multi-tenant-agents.md). Code: [github.com/Vivek0712/awesome-microvm/tree/main/examples/multi-tenant-agents](https://github.com/Vivek0712/awesome-microvm/tree/main/examples/multi-tenant-agents). Live transcript: [demo-multi-tenant-agents.png](../../blog/img/demo-multi-tenant-agents.png).
