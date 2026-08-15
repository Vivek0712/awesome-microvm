"""One-shot CI runner: clone -> lint -> test -> report.

POST /job {"repo_url": "...", "ref": "main", "steps": ["ruff check .", "pytest -q"]}

Runs each job in a VM nobody else has ever touched. The orchestrator launches
with a short maximumDurationInSeconds and terminates on completion — per-second
billing means a 90-second test run costs a fraction of a cent, and a poisoned
dependency chain dies with the VM.
"""

import json
import os
import subprocess
import time

from microvm_hooks import HookApp

app = HookApp()
JOB_DIR = "/tmp/job"


@app.on_ready
def ready(_ctx):
    subprocess.run(["git", "--version"], capture_output=True, check=True)
    return True


@app.on_run
def on_run(ctx):
    # Per-job context (repo, ref) can arrive as runHookPayload for push-style dispatch.
    payload = ctx.get("runHookPayload")
    if payload:
        with open("/tmp/assignment.json", "w") as f:
            f.write(payload)


def _sh(cmd: str, cwd: str, timeout: int = 600) -> dict:
    started = time.time()
    proc = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                          timeout=timeout, cwd=cwd)
    return {
        "cmd": cmd,
        "exit_code": proc.returncode,
        "duration_s": round(time.time() - started, 1),
        "output": (proc.stdout + proc.stderr)[-15_000:],
    }


@app.route("POST", "/job")
def job(body, _headers):
    repo, ref = body.get("repo_url"), body.get("ref", "main")
    if not repo:
        return 400, {"error": "need 'repo_url'"}
    subprocess.run(["rm", "-rf", JOB_DIR])
    clone = _sh(f"git clone --depth 1 --branch {ref} {repo} {JOB_DIR}", "/tmp", timeout=300)
    if clone["exit_code"] != 0:
        return 500, {"stage": "clone", **clone}
    steps = [
        _sh(step, JOB_DIR)
        for step in body.get("steps", ["python3.12 -m pytest -q"])
    ]
    passed = all(s["exit_code"] == 0 for s in steps)
    return 200, {"passed": passed, "clone_s": clone["duration_s"], "steps": steps}


if __name__ == "__main__":
    app.serve(port=8080)
