# HTML → PDF service

Untrusted markup rendered inside the VM boundary (WeasyPrint). Idle policy suspends between bursts; auto-resume wakes it on the next render.

```console
mvm image build pdf-service examples/pdf-service
mvm run pdf-service --wait
mvm call <id> /render -X POST -d '{"html":"<h1>Invoice</h1>"}'
```

Deep dive: [blog post](../../blog/07-pdf-service.md) · live transcript: [screenshot](../../benchmarks/results/demo-pdf-service.svg)
