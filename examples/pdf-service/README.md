# HTML to PDF service

Untrusted markup rendered inside the VM boundary with WeasyPrint. The idle policy suspends the VM between bursts, and auto-resume wakes it on the next render.

```console
mvm image build pdf-service examples/pdf-service
mvm run pdf-service --wait
mvm call <id> /render -X POST -d '{"html":"<h1>Invoice</h1>"}'
```

Series: [part 2, section 7 of Building on AWS Lambda MicroVMs](https://builder.aws.com/content/3JJ2oNWY9EsZzivMMx044cSlrFQ/seven-workloads-lambda-could-never-run-until-microvms). Full write-up: [An HTML to PDF service on AWS Lambda MicroVMs that sleeps between bursts](../../blog/deep-dives/07-pdf-service.md). Code: [github.com/Vivek0712/awesome-microvm/tree/main/examples/pdf-service](https://github.com/Vivek0712/awesome-microvm/tree/main/examples/pdf-service). Live transcript: [demo-pdf-service.png](../../blog/img/demo-pdf-service.png). The rendered invoice from the demo is [demo-invoice.pdf](../../benchmarks/results/demo-invoice.pdf).
