# Part 6: form fields

Title (152 chars):
Durable functions meet Lambda MicroVMs in the AWS SDK: I ran microvm and microvmSession live, then served them from a Python worker built on microvm-ctl

Description (423 chars):
The AWS durable execution SDK for JavaScript now has MicroVM operations: microvm for one job, microvmSession for several jobs on one VM, and a Node worker for the code inside. I built them, ran the unit tests and the full end-to-end suite live, and then ran the same suite against a Python worker built on microvm-ctl. 382 of 382 unit tests, 13 of 13 scenarios on both workers, and one quota failure every fan-out will hit.

Tags: ["lambda", "durable-functions", "typescript", "python", "serverless"]
Series: Building on AWS Lambda MicroVMs
Cover image: blog/img/cover-05.png

Figures, in body order. Each < FIGURE n of N > marker in the body is replaced by the upload; the
Alt text line under it goes into the image's alt field; the italic line after it is the caption.
  1. blog/img/hook-05-durable-contract.png  (under "(top)")
