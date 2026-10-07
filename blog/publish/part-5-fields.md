# Part 5: form fields

Title (118 chars):
Strands Box on AWS Lambda MicroVMs: a kernel per agent, a policy per command, and a fleet to run them with microvm-ctl

Description (351 chars):
Strands Box checks every shell command, file and request an agent makes against a Dogwood policy. Lambda MicroVMs give the agent its own kernel. I built Box for aarch64 Linux, ran it inside Lambda MicroVMs, and drove fleets with microvm-ctl: 83 ms per box, 22.7 boxes/s per 1 GB VM, 64 of 64 correct across 8 VMs, a playground app, and the bugs I hit.

Tags: ["lambda", "strands-agents", "security", "firecracker", "python"]
Series: Building on AWS Lambda MicroVMs
Cover image: blog/img/cover-04.png

Figures, in body order. Each < FIGURE n of N > marker in the body is replaced by the upload; the
Alt text line under it goes into the image's alt field; the italic line after it is the caption.
  1. blog/img/hook-04-strands-box.png  (under "(top)")
  2. blog/img/arch-10-box-in-vm.png  (under "One task, one box")
  3. blog/img/playground-box-bench.png  (under "The numbers for one VM")
  4. blog/img/playground-box-fanout.png  (under "A fleet with microvm-ctl")
  5. blog/img/playground-box-run.png  (under "The playground")
  6. blog/img/playground-box-writes.png  (under "The playground")
