"""Handoff agent: the shared image every orchestrator leases for one task.

The orchestrator launches this image with a lease in runHookPayload:

    {"lease": {"kind": "sfn|durable|http|sqs|eventbridge|none", "token": "...", "id": "..."},
     "task":  {"steps": ["<shell>", ...], "workdir": "/tmp/job", "env": {"K": "V"}}}

The hook runtime decodes the lease, answers /run at once, heartbeats while `work` runs
in a thread, and delivers the return value (or a raised LeaseError) through the lease's
completer. Each step runs with `bash -c`; the first non-zero exit fails the lease with
`StepFailed`. Test aids: task["hang_s"] sleeps that long after the steps (heartbeat and
timeout drills); task["fail_after_s"] sleeps then fails with a retryable `Injected` error.
GET /status (built in) shows phase, progress, heartbeats, and the log tail.
"""

import os
import subprocess
import time

from microvm_hooks import HookApp, LeaseError

app = HookApp()
DEFAULT_WORKDIR = "/tmp/job"
STEP_TIMEOUT_S = int(os.environ.get("STEP_TIMEOUT_S", "600"))
TAIL_CHARS = 2000


@app.on_ready
def ready(_ctx):
    subprocess.run(["bash", "-c", "true"], check=True)
    return True


def _step(cmd: str, cwd: str, env: dict) -> dict:
    started = time.time()
    try:
        proc = subprocess.run(["bash", "-c", cmd], cwd=cwd, env=env, capture_output=True, text=True,
                              timeout=STEP_TIMEOUT_S, check=False)
        code, out = proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        code, out = 124, f"timed out after {STEP_TIMEOUT_S} s"
    return {"cmd": cmd, "exit_code": code, "duration_s": round(time.time() - started, 3),
            "output_tail": out[-TAIL_CHARS:]}


@app.on_lease
def work(task: dict, lease) -> dict:
    steps = task.get("steps") or []
    if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
        raise LeaseError("BadTask", "task.steps must be a list of shell strings", data={"steps": steps})
    workdir = task.get("workdir") or DEFAULT_WORKDIR
    os.makedirs(workdir, exist_ok=True)
    env = dict(os.environ, **{str(k): str(v) for k, v in (task.get("env") or {}).items()})
    n, results = len(steps), []
    lease.job.progress(0, n)
    for i, cmd in enumerate(steps):
        lease.check()  # the orchestrator stopped waiting: do not spend on the next step
        lease.job.phase(f"step {i + 1}/{n}")
        lease.job.log(f"$ {cmd}")
        r = _step(cmd, workdir, env)
        results.append(r)
        lease.job.log(r["output_tail"][-400:].rstrip() or "(no output)", exit_code=r["exit_code"],
                      duration_s=r["duration_s"])
        lease.job.progress(i + 1, n)
        if r["exit_code"] != 0:
            raise LeaseError("StepFailed", f"step {i + 1}/{n} exited {r['exit_code']}: {cmd}",
                             retryable=False, data={"step": i, "exit_code": r["exit_code"],
                                                    "output_tail": r["output_tail"][-800:], "steps": results})
    if task.get("hang_s"):
        lease.job.phase("hang")
        time.sleep(float(task["hang_s"]))
    if task.get("fail_after_s") is not None:
        lease.job.phase("injected failure")
        time.sleep(float(task["fail_after_s"]))
        raise LeaseError("Injected", f"failed on purpose after {task['fail_after_s']} s", retryable=True,
                         data={"steps": results})
    lease.job.phase("done")
    result = {"passed": True, "steps": results, "microvm_id": lease.microvm_id, "heartbeats": lease.heartbeats}
    lease.job.log(f"passed {n} step(s)", result=dict(result, steps=[
        {k: r[k] for k in ("cmd", "exit_code", "duration_s")} for r in results]))
    return result


if __name__ == "__main__":
    app.serve(port=8080)
