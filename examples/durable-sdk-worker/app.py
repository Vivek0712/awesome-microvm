"""The app of the AWS durable execution SDK's MicroVM end-to-end test, written in Python on microvm-ctl's
hook runtime instead of in Node on @aws/durable-execution-sdk-js-microvm-worker.

It mirrors packages/aws-durable-execution-sdk-js-extras/e2e/microvm-app.ts route for route, so the SDK's own
e2e scenarios can run against it unchanged: the durable function in TypeScript calls microvm() and
microvmSession(), and this process in the MicroVM speaks the same job contract.
"""
import json
import os
import platform
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from microvm_hooks import HookApp, LeaseError

app = HookApp()
SESSION_DIR = Path("/tmp/session")
remembered: dict = {}
resume_hooks = 0


@app.on_resume
def _resume(_ctx):
    global resume_hooks
    resume_hooks += 1


def run_job(job, ctx):
    ctx.job.log("job received", mode=job.get("mode"), label=job.get("label"))
    if job.get("mode") == "crash":
        # exits after the first heartbeat; the durable function must fail on the heartbeat timeout
        threading.Timer(2.0, lambda: os._exit(1)).start()
        threading.Event().wait()
    ctx.cancelled.wait(job.get("sleepSeconds", 0))
    if job.get("mode") == "fail":
        raise LeaseError("Error", f"job {job.get('label')} failed on purpose")
    return {
        "label": job.get("label"),
        "microvmId": ctx.microvm_id or app.microvm_id,
        "node": f"python {platform.python_version()} (microvm-ctl hook runtime)",
        "inputLength": len(json.dumps(job, separators=(",", ":"), ensure_ascii=False)),
        "padding": job.get("padding"),
        "finishedAt": datetime.now(timezone.utc).isoformat(),
    }


app.durable_job(run_job)
app.durable_job(run_job, path="/job")


@app.durable_job(path="/write")
def write(inp, ctx):
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    (SESSION_DIR / inp["key"]).write_text(inp["value"])
    return {"microvmId": ctx.microvm_id or app.microvm_id}


@app.durable_job(path="/read")
def read(inp, ctx):
    return {"microvmId": ctx.microvm_id or app.microvm_id, "value": (SESSION_DIR / inp["key"]).read_text()}


@app.durable_job(path="/remember")
def remember(inp, ctx):
    remembered.update(value=inp["value"], storedAt=datetime.now(timezone.utc).isoformat())
    return {"microvmId": ctx.microvm_id or app.microvm_id, "pid": os.getpid()}


START = time.monotonic()


@app.durable_job(path="/recall")
def recall(_inp, ctx):
    return {"microvmId": ctx.microvm_id or app.microvm_id, "pid": os.getpid(),
            "uptimeSeconds": round(time.monotonic() - START), "resumeHooks": resume_hooks, **remembered}


if __name__ == "__main__":
    app.serve(port=8080)
