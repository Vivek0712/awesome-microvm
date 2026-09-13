# HTML to PDF service

Untrusted markup rendered inside the VM boundary with WeasyPrint. The idle policy suspends the VM between bursts, and auto-resume wakes it on the next render.

```console
mvm image build pdf-service examples/pdf-service
mvm run pdf-service --wait
mvm call <id> /render -X POST -d '{"html":"<h1>Invoice</h1>"}'
```

Article: [An HTML to PDF service on AWS Lambda MicroVMs that sleeps between bursts](../../blog/07-pdf-service.md). Live transcript: [demo-pdf-service.png](../../blog/img/demo-pdf-service.png). The rendered invoice from the demo is [demo-invoice.pdf](../../benchmarks/results/demo-invoice.pdf).
