# Part 7: form fields

Title (136 chars):
Lambda Web Functions as the front door to Lambda MicroVMs: launch a job, stream its log, and take the VM's callback on the same endpoint

Description (409 chars):
Lambda Web Functions launched this week: a Node HTTP server that Lambda runs and scales, behind its own HTTPS endpoint. I used one as the front door and the orchestrator for Lambda MicroVMs. It launches a VM per job with a microvm-ctl HTTP lease that points back at itself, streams the VM's log to the caller, records the result the VM sends, and terminates it. The VM image is the one from part 4, unchanged.

Tags: ["lambda", "serverless", "nodejs", "microvms", "architecture"]
Series: Building on AWS Lambda MicroVMs
Cover image: blog/img/cover-06.png

Figures, in body order. Each < FIGURE n of N > marker in the body is replaced by the upload; the
Alt text line under it goes into the image's alt field; the italic line after it is the caption.
  1. blog/img/hook-06-web-handoff.png  (under "(top)")
  2. blog/img/demo-web-handoff.png  (under "The handoff")
