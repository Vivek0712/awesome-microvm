# Part 4: form fields

Title (108 chars):
Hand a task to a MicroVM from anywhere: one lease, Step Functions, durable functions, or your own controller

Description (595 chars):
Orchestrators want to hand a VM a job and wait. The lease contract in microvm-ctl puts the callback token in runHookPayload so the VM heartbeats and completes the task itself, with no polling, no endpoint call, and no token mint. The same 2 GB agent image leased from a Step Functions state machine, a Lambda durable function, SQS, EventBridge, and a plain HTTP collector, timed and priced on the live service: 5.9 to 10.8 s end to end and under a twentieth of a cent per lease, then fanned out to eight VMs at once under a plan the plane sizes from the account's quota before anything launches.

Tags: ["lambda", "step-functions", "serverless", "firecracker", "python"]
Series: Building on AWS Lambda MicroVMs
Cover image: blog/img/cover-03.png

Images to upload, in body order (each replaces its < upload ... > marker in the body):
  1. blog/img/playground-lease.png
  2. blog/img/demo-sfn-terminate-failed.png
  3. blog/img/demo-durable-hang.png
  4. blog/img/arch-09-fanout-map.png
  5. blog/img/playground-fanout.png
  6. blog/img/handoff-bench.png
  7. blog/img/fleet-watch.png
  8. blog/img/demo-bench.png
  9. blog/img/demo-playground-fleet-jobs.png
