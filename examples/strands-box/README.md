# strands-box

[Strands Box](https://github.com/strands-agents/box) inside Lambda MicroVMs: every task runs in a fresh box, so a Dogwood policy decides each shell command, each file a command touches, and each outbound request the agent makes. The code lives in its own repository, [strands-box-microvm-ctl](https://github.com/Vivek0712/strands-box-microvm-ctl), because it builds Box from source for aarch64 Linux and carries a playground web app and a CloudFront stack.

| | |
|---|---|
| Image | `al2023-minimal`, 1 GB, Box 0.1.0 built in `amazonlinux:2023` arm64, the Strands Agents SDK example agent and a scripted agent with the same three tools |
| Build flag that matters | `mvm image build strands-box image --memory 1024 --caps-all`; without `additionalOsCapabilities: ["ALL"]` Box cannot mount the agent's `/proc` and every box fails |
| One box, warm, in the VM | 83 ms p50 for five tool calls and 26 policy decisions |
| One VM | 22.7 boxes per second from 4 boxes at once, flat to 32 |
| Eight VMs | 64 of 64 correct per round, 1,664 decisions and 192 denials a round |
| Fleet tools | `Fleet.scale_to`, `Fleet.dispatch` and `mvm dispatch` (new in microvm-ctl 0.4.0), `plan` and `lease_many`, suspend, resume, drain |

```sh
git clone https://github.com/Vivek0712/strands-box-microvm-ctl && cd strands-box-microvm-ctl
./build/build-box.sh
mvm image build strands-box image --memory 1024 --caps-all
mvm run strands-box --wait
mvm call <microvm-id> /task -X POST -d '{"steps": ["read:README.md", "read:.env", "run:rm scratch.txt"]}'
```

The write-up is part 5 of the series, [blog/04-strands-box.md](../../blog/04-strands-box.md). What broke along the way is in [FINDINGS.md](https://github.com/Vivek0712/strands-box-microvm-ctl/blob/main/FINDINGS.md).
