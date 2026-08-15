"""Notebook-style kernel: a persistent Python namespace per microVM.

POST /cell {"code": "x = 41"}      -> {"cell": 1, ...}
POST /cell {"code": "x + 1"}       -> {"value": "42", ...}   # state persisted
GET  /kernel                       -> namespace summary + uptime + PID

Suspend the VM for hours, come back, `x` is still 42 — same process, same
PID. That's the demo that sells the suspend/resume economics.
"""

import contextlib
import io
import os
import time
import uuid

from microvm_hooks import HookApp

app = HookApp()
NS: dict = {}
KERNEL = {"id": None, "started": time.time(), "cells": 0}


@app.on_ready
def ready(_ctx):
    import numpy, pandas  # noqa: F401 — imported into the snapshot, warm for every clone
    return True


@app.on_validate
def validate(_ctx):
    exec("import numpy as np; _ = np.arange(10).mean()", {})


@app.on_run
def on_run(ctx):
    KERNEL["id"] = ctx.get("microvmId") or str(uuid.uuid4())[:8]
    KERNEL["started"] = time.time()


@app.route("POST", "/cell")
def cell(body, _headers):
    code = body.get("code", "")
    KERNEL["cells"] += 1
    out = io.StringIO()
    value = None
    try:
        with contextlib.redirect_stdout(out):
            try:
                value = eval(compile(code, "<cell>", "eval"), NS)  # expression?
            except SyntaxError:
                exec(compile(code, "<cell>", "exec"), NS)
    except Exception as e:
        return 400, {"cell": KERNEL["cells"], "error": f"{type(e).__name__}: {e}",
                     "stdout": out.getvalue()}
    return 200, {
        "cell": KERNEL["cells"],
        "value": repr(value) if value is not None else None,
        "stdout": out.getvalue(),
        "kernel": KERNEL["id"],
    }


@app.route("GET", "/kernel")
def kernel(_body, _headers):
    return 200, {
        "kernel": KERNEL["id"],
        "pid": os.getpid(),
        "uptime_s": round(time.time() - KERNEL["started"], 1),
        "cells_executed": KERNEL["cells"],
        "variables": {k: type(v).__name__ for k, v in NS.items() if not k.startswith("__")},
    }


if __name__ == "__main__":
    app.serve(port=8080)
