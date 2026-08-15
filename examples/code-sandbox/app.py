"""Sandboxed code executor.

POST /execute {"code": "...", "timeout": 30}
  -> {"stdout": ..., "stderr": ..., "exit_code": ..., "duration_ms": ...}

State persists across calls within one microVM (files under /tmp/workspace,
installed packages), survives suspend/resume, and is destroyed on terminate —
exactly the sandbox contract agents want.
"""

import json
import os
import subprocess
import sys
import time
import uuid

from microvm_hooks import HookApp

app = HookApp()
WORKSPACE = "/tmp/workspace"
SESSION = {"id": None, "executions": 0}


@app.on_ready
def ready(_ctx):
    os.makedirs(WORKSPACE, exist_ok=True)
    return True  # warm: interpreter up, deps imported at module load


@app.on_validate
def validate(_ctx):
    # Exercise the hot path on a fresh VM so Lambda prefetches these
    # snapshot regions — measurably faster first /execute after launch.
    _run_code("import numpy, pandas; print(numpy.zeros(4).sum())", 20)


@app.on_run
def on_run(ctx):
    # Fresh identity per clone — anything build-time is shared by every VM.
    SESSION["id"] = ctx.get("microvmId") or str(uuid.uuid4())
    SESSION["executions"] = 0


@app.on_suspend
def on_suspend(_ctx):
    sys.stdout.flush()


def _run_code(code: str, timeout: int) -> dict:
    started = time.time()
    proc = subprocess.run(
        ["python3.12", "-c", code],
        capture_output=True, text=True, timeout=timeout, cwd=WORKSPACE,
    )
    return {
        "stdout": proc.stdout[-50_000:],
        "stderr": proc.stderr[-50_000:],
        "exit_code": proc.returncode,
        "duration_ms": round((time.time() - started) * 1000, 1),
    }


@app.route("POST", "/execute")
def execute(body, _headers):
    code = body.get("code")
    if not code:
        return 400, {"error": "body must be {\"code\": \"...\"}"}
    try:
        result = _run_code(code, int(body.get("timeout", 30)))
    except subprocess.TimeoutExpired:
        return 408, {"error": "execution timed out"}
    SESSION["executions"] += 1
    result |= {"session": SESSION["id"], "execution_count": SESSION["executions"]}
    return 200, result


@app.route("POST", "/pip")
def pip_install(body, _headers):
    pkgs = body.get("packages", [])
    proc = subprocess.run(
        ["python3.12", "-m", "pip", "install", "--no-cache-dir", *pkgs],
        capture_output=True, text=True, timeout=300,
    )
    return (200 if proc.returncode == 0 else 500), {"log": proc.stdout[-5000:] + proc.stderr[-3000:]}


@app.route("GET", "/state")
def state(_body, _headers):
    return 200, {
        "session": SESSION["id"],
        "executions": SESSION["executions"],
        "workspace_files": sorted(os.listdir(WORKSPACE)),
        "pid": os.getpid(),
    }


if __name__ == "__main__":
    app.serve(port=8080)
