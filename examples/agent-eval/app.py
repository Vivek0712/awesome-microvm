"""Agent-eval worker: score an agent's answer inside a pristine clone.

POST /evaluate {"task_id": ..., "setup": "...", "candidate_code": "...", "tests": "..."}
  -> {"task_id", "passed", "score", "log", "environment_fingerprint"}

The harness pattern (see examples/agent-eval/harness.py) launches N of these
VMs in parallel via Fleet.scale_to(N), routes one eval task to each with
per-VM runHookPayload, collects scores, and drains the fleet — a fresh,
identical environment per run is exactly what RL/eval pipelines pay E2B for.
"""

import json
import os
import subprocess
import uuid

from microvm_hooks import HookApp

app = HookApp()
WORK = "/tmp/eval"
IDENTITY = {"worker": None, "assignment": None}


@app.on_ready
def ready(_ctx):
    os.makedirs(WORK, exist_ok=True)
    return True


@app.on_run
def on_run(ctx):
    IDENTITY["worker"] = str(uuid.uuid4())[:8]
    payload = ctx.get("runHookPayload")
    IDENTITY["assignment"] = json.loads(payload) if payload else None  # e.g. {"shard": 3}


def _sh(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=WORK)


@app.route("POST", "/evaluate")
def evaluate(body, _headers):
    for f in os.listdir(WORK):  # each task starts from a clean slate
        path = os.path.join(WORK, f)
        subprocess.run(["rm", "-rf", path])
    if body.get("setup"):
        setup = _sh(["bash", "-c", body["setup"]])
        if setup.returncode != 0:
            return 500, {"error": "setup failed", "log": setup.stderr[-3000:]}
    with open(os.path.join(WORK, "candidate.py"), "w") as f:
        f.write(body.get("candidate_code", ""))
    with open(os.path.join(WORK, "test_candidate.py"), "w") as f:
        f.write(body.get("tests", ""))
    proc = _sh(["python3.12", "-m", "pytest", "test_candidate.py", "-q", "--tb=line"])
    lines = proc.stdout.strip().splitlines()
    summary = lines[-1] if lines else ""
    passed = proc.returncode == 0
    return 200, {
        "task_id": body.get("task_id"),
        "passed": passed,
        "summary": summary,
        "log": proc.stdout[-10_000:],
        "worker": IDENTITY["worker"],
        "assignment": IDENTITY["assignment"],
    }


if __name__ == "__main__":
    app.serve(port=8080)
