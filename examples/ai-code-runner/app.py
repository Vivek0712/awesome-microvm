"""AI code runner: prompt in, verified working code + output out.

POST /solve {"task": "plot a sine wave and save it", "max_iterations": 3}

The loop lives *inside* the microVM: it calls Bedrock through the VM's
execution role (no API keys in the image — snapshots turn RAM into stored
data, so nothing secret is ever baked in), executes the generated code
locally, and feeds tracebacks to the model until the code runs clean.
"""

import json
import os
import subprocess
import time

from microvm_hooks import HookApp

app = HookApp()
MODEL = os.environ.get("MODEL_ID", "us.amazon.nova-lite-v1:0")
WORKSPACE = "/tmp/workspace"
_bedrock = None  # created lazily in /run — never at build time (dead conns in snapshot)


@app.on_ready
def ready(_ctx):
    os.makedirs(WORKSPACE, exist_ok=True)
    return True


@app.on_run
def on_run(_ctx):
    global _bedrock
    import boto3
    _bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))


@app.on_resume
def on_resume(ctx):
    on_run(ctx)  # refresh the client: pre-suspend connections may be dead


def _generate(messages: list[dict]) -> str:
    resp = _bedrock.converse(
        modelId=MODEL,
        messages=messages,
        system=[{"text": "Reply with a single runnable Python script only, no prose, no fences."}],
        inferenceConfig={"maxTokens": 2000, "temperature": 0.2},
    )
    text = resp["output"]["message"]["content"][0]["text"]
    return text.replace("```python", "").replace("```", "").strip()


def _execute(code: str) -> dict:
    path = os.path.join(WORKSPACE, "solution.py")
    with open(path, "w") as f:
        f.write(code)
    started = time.time()
    proc = subprocess.run(
        ["python3.12", path], capture_output=True, text=True, timeout=60, cwd=WORKSPACE
    )
    return {
        "stdout": proc.stdout[-20_000:],
        "stderr": proc.stderr[-20_000:],
        "exit_code": proc.returncode,
        "duration_ms": round((time.time() - started) * 1000, 1),
    }


@app.route("POST", "/solve")
def solve(body, _headers):
    task = body.get("task")
    if not task:
        return 400, {"error": "body must include 'task'"}
    messages = [{"role": "user", "content": [{"text": f"Task: {task}"}]}]
    attempts = []
    for i in range(int(body.get("max_iterations", 3))):
        code = _generate(messages)
        result = _execute(code)
        attempts.append({"iteration": i + 1, "exit_code": result["exit_code"],
                         "duration_ms": result["duration_ms"]})
        if result["exit_code"] == 0:
            return 200, {
                "solved": True, "iterations": i + 1, "code": code,
                "stdout": result["stdout"], "attempts": attempts,
                "artifacts": sorted(os.listdir(WORKSPACE)),
            }
        messages.append({"role": "assistant", "content": [{"text": code}]})
        messages.append({"role": "user", "content": [
            {"text": f"That failed:\n{result['stderr'][-3000:]}\nFix it. Full script only."}
        ]})
    return 200, {"solved": False, "attempts": attempts, "last_error": result["stderr"][-3000:]}


if __name__ == "__main__":
    app.serve(port=8080)
