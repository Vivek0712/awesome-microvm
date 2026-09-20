# Part 4: form fields

Title (108 chars):
Hand a task to a MicroVM from anywhere: one lease, Step Functions, durable functions, or your own controller

Description (477 chars):
Orchestrators want to hand a VM a job and wait. The lease contract in microvm-ctl puts the callback token in runHookPayload so the VM heartbeats and completes the task itself, with no polling, no endpoint call, and no token mint. The same 2 GB agent image leased from a Step Functions state machine, a Lambda durable function, SQS, EventBridge, and a plain HTTP collector, timed and priced on the live service: 5.7 to 8.9 s end to end and under a thirtieth of a cent per lease.

Tags: ["lambda", "step-functions", "serverless", "firecracker", "python"]
Series: Building on AWS Lambda MicroVMs
Cover image: blog/img/cover-03.png

Images to upload, in body order (each replaces its < upload ... > marker in the body):
  1. blog/img/playground-lease.png
  2. blog/img/handoff-bench.png
