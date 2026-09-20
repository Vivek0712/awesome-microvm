"""Handoff agent: the shared image every orchestrator leases for one task.

The orchestrator launches this image with a lease in runHookPayload:

    {"lease": {"kind": "sfn|durable|http|sqs|eventbridge|none", "token": "...", "id": "..."},
     "task":  {"steps": ["<shell>", ...], "workdir": "/tmp/job", "env": {"K": "V"},
               "parallel": false, "max_parallel": 4}}

The hook runtime decodes the lease, answers /run at once, heartbeats while `work` runs
in a thread, and delivers the return value (or a raised LeaseError) through the lease's
completer. Each step runs with `bash -c`. Sequential (the default): the first non-zero
exit fails the lease with `StepFailed`. Parallel (`"parallel": true`): steps run on a
thread pool of `max_parallel` workers (default: the VM's CPU count, or 4), progress counts
completions, results keep step order, and one `StepFailed` listing every failing step is
raised after all of them finish. Test aids: task["hang_s"] sleeps that long after the steps;
task["fail_after_s"] sleeps then fails with a retryable `Injected` error. GET /status is built in.
"""

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

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


def _report(lease, i: int, r: dict) -> None:
    lease.job.log(r["output_tail"][-400:].rstrip() or "(no output)", step=i + 1, exit_code=r["exit_code"],
                  duration_s=r["duration_s"])


def _failure(i: int, r: dict) -> dict:
    return {"step": i, "exit_code": r["exit_code"], "cmd": r["cmd"], "output_tail": r["output_tail"][-800:]}


def _sequential(steps: list, cwd: str, env: dict, lease) -> list:
    n, results = len(steps), []
    for i, cmd in enumerate(steps):
        lease.check()  # the orchestrator stopped waiting: do not spend on the next step
        lease.job.phase(f"step {i + 1}/{n}")
        lease.job.log(f"$ {cmd}")
        r = _step(cmd, cwd, env)
        results.append(r)
        _report(lease, i, r)
        lease.job.progress(i + 1, n)
        if r["exit_code"] != 0:
            raise LeaseError("StepFailed", f"step {i + 1}/{n} exited {r['exit_code']}: {cmd}",
                             retryable=False, data={**_failure(i, r), "steps": results})
    return results


def _parallel(steps: list, cwd: str, env: dict, lease, workers: int) -> list:
    n, results, done = len(steps), [None] * len(steps), 0
    lease.check()
    lease.job.phase(f"parallel 0/{n} done")
    lease.job.log(f"running {n} steps on {workers} workers", steps=steps)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_step, cmd, cwd, env): i for i, cmd in enumerate(steps)}
        for fut in as_completed(futures):
            i = futures[fut]
            results[i] = fut.result()
            done += 1
            _report(lease, i, results[i])
            lease.job.progress(done, n)
            lease.job.phase(f"parallel {done}/{n} done")
    failed = [i for i, r in enumerate(results) if r["exit_code"] != 0]
    if failed:
        raise LeaseError("StepFailed", f"{len(failed)} of {n} parallel steps failed: {[i + 1 for i in failed]}",
                         retryable=False, data={"failed": [_failure(i, results[i]) for i in failed],
                                                "steps": results})
    return results


@app.on_lease
def work(task: dict, lease) -> dict:
    steps = task.get("steps") or []
    if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
        raise LeaseError("BadTask", "task.steps must be a list of shell strings", data={"steps": steps})
    workdir = task.get("workdir") or DEFAULT_WORKDIR
    os.makedirs(workdir, exist_ok=True)
    env = dict(os.environ, **{str(k): str(v) for k, v in (task.get("env") or {}).items()})
    n, parallel = len(steps), bool(task.get("parallel"))
    lease.job.progress(0, n)
    if parallel:
        workers = max(1, int(task.get("max_parallel") or os.cpu_count() or 4))
        results = _parallel(steps, workdir, env, lease, workers)
    else:
        results = _sequential(steps, workdir, env, lease)
    if task.get("hang_s"):
        lease.job.phase("hang")
        time.sleep(float(task["hang_s"]))
    if task.get("fail_after_s") is not None:
        lease.job.phase("injected failure")
        time.sleep(float(task["fail_after_s"]))
        raise LeaseError("Injected", f"failed on purpose after {task['fail_after_s']} s", retryable=True,
                         data={"steps": results})
    lease.job.phase("done")
    result = {"passed": True, "steps": results, "parallel": parallel, "microvm_id": lease.microvm_id,
              "heartbeats": lease.heartbeats}
    lease.job.log(f"passed {n} step(s){' in parallel' if parallel else ''}", result=dict(result, steps=[
        {k: r[k] for k in ("cmd", "exit_code", "duration_s")} for r in results]))
    return result


if __name__ == "__main__":
    app.serve(port=8080)
