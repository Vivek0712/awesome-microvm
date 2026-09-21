# Part 2: form fields

Title (54 chars):
Seven workloads Lambda could never run, until MicroVMs

Description (373 chars):
Untrusted code with a persistent filesystem, an agent that lives for hours, a kernel that sleeps for free, a database engine per user, a CI runner nobody has touched, a renderer that wakes on demand. Seven customer-shaped workloads that needed a server, rebuilt on AWS Lambda MicroVMs and measured: 3.5 second launches, 93.8% cheaper sessions, under half a cent per CI job.

Tags: ["lambda", "firecracker", "python", "ai", "sandbox"]
Series: Building on AWS Lambda MicroVMs
Cover image: blog/img/cover-01.png

Figures, in body order. Each < FIGURE n of N > marker in the body is replaced by the upload; the
Alt text line under it goes into the image's alt field; the italic line after it is the caption.
  1. blog/img/arch-01-code-sandbox.png  (under "1. A code execution sandbox")
  2. blog/img/demo-code-sandbox.png  (under "1. A code execution sandbox")
  3. blog/img/demo-ai-code-runner.png  (under "2. An AI code runner with a self-repair loop")
  4. blog/img/arch-03-agent-eval.png  (under "3. An agent evaluation fleet")
  5. blog/img/demo-agent-eval.png  (under "3. An agent evaluation fleet")
  6. blog/img/demo-notebook.png  (under "4. A notebook kernel that suspends for free")
  7. blog/img/arch-05-data-analytics.png  (under "5. Sandboxed DuckDB analytics")
  8. blog/img/demo-data-analytics.png  (under "5. Sandboxed DuckDB analytics")
  9. blog/img/demo-ci-runner.png  (under "6. An ephemeral CI runner")
  10. blog/img/demo-pdf-service.png  (under "7. An HTML to PDF service that sleeps between bursts")
  11. blog/img/mvm-cost.png  (under "What they cost")
