"""Per-tenant agent: one isolated Bedrock-backed assistant per customer.

The controller launches one VM per tenant with
runHookPayload='{"tenant_id": "acme", "display_name": "Acme Corp"}' — the
image never contains tenant data (image env vars are shared by every clone,
so per-tenant values there are the #1 anti-pattern). Conversation memory
lives in VM memory and survives suspend/resume; the idle policy makes an
inactive tenant cost cents per month.

POST /chat {"message": "..."}   GET /whoami
"""

import json
import os
import time

from microvm_hooks import HookApp

app = HookApp()
TENANT: dict = {}
HISTORY: list[dict] = []
_bedrock = None
MODEL = os.environ.get("MODEL_ID", "us.amazon.nova-lite-v1:0")


@app.on_ready
def ready(_ctx):
    return True


@app.on_run
def on_run(ctx):
    global _bedrock
    payload = ctx.get("runHookPayload")
    TENANT.update(json.loads(payload) if payload else {"tenant_id": "unassigned"})
    import boto3
    _bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))


@app.on_resume
def on_resume(ctx):
    global _bedrock
    import boto3  # refresh clients; pre-suspend connections may be stale
    _bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))


@app.route("POST", "/chat")
def chat(body, _headers):
    msg = body.get("message")
    if not msg:
        return 400, {"error": "need 'message'"}
    HISTORY.append({"role": "user", "content": [{"text": msg}]})
    started = time.time()
    resp = _bedrock.converse(
        modelId=MODEL,
        messages=HISTORY[-20:],
        system=[{"text": f"You are the private assistant of tenant {TENANT.get('display_name') or TENANT.get('tenant_id')}. Be concise."}],
        inferenceConfig={"maxTokens": 500},
    )
    answer = resp["output"]["message"]["content"][0]["text"]
    HISTORY.append({"role": "assistant", "content": [{"text": answer}]})
    return 200, {
        "tenant": TENANT.get("tenant_id"),
        "reply": answer,
        "turns": len(HISTORY) // 2,
        "latency_ms": round((time.time() - started) * 1000),
    }


@app.route("GET", "/whoami")
def whoami(_body, _headers):
    return 200, {"tenant": TENANT, "conversation_turns": len(HISTORY) // 2, "pid": os.getpid()}


if __name__ == "__main__":
    app.serve(port=8080)
