"""GitHub webhook receiver behind a Function URL: verify, normalize, start the durable execution.

Not durable itself. Its whole job is to turn "a PR changed" into exactly one durable
execution: the execution name is derived from repo, PR, and head SHA, so GitHub's
redelivery of the same event reattaches instead of leasing a second microVM.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

import boto3

ORCHESTRATOR = os.environ["ORCHESTRATOR_FUNCTION"]           # qualified: name:alias
SECRET_ARN = os.environ.get("WEBHOOK_SECRET_ARN")
_lambda = boto3.client("lambda")
_secret: str | None = None


def _webhook_secret() -> str | None:
    global _secret
    if _secret is None and SECRET_ARN:
        _secret = boto3.client("secretsmanager").get_secret_value(SecretId=SECRET_ARN)["SecretString"]
    return _secret


def _verify(raw: bytes, signature: str | None) -> bool:
    secret = _webhook_secret()
    if not secret:
        return True                                          # no secret configured: accept (dev only)
    if not signature or not signature.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[7:], digest)


def handler(event: dict, _context) -> dict:
    raw = event.get("body") or ""
    raw = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not _verify(raw, headers.get("x-hub-signature-256")):
        return {"statusCode": 401, "body": "bad signature"}
    if headers.get("x-github-event") != "pull_request":
        return {"statusCode": 204, "body": ""}
    body = json.loads(raw)
    if body.get("action") not in ("opened", "synchronize", "reopened"):
        return {"statusCode": 204, "body": ""}
    pr, repo = body["pull_request"], body["repository"]
    name = f"{repo['full_name']}-{pr['number']}-{pr['head']['sha'][:12]}".replace("/", "-")
    name = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)[:64]
    try:
        resp = _lambda.invoke(FunctionName=ORCHESTRATOR, InvocationType="Event",
                              Payload=raw, DurableExecutionName=name)
        return {"statusCode": 202, "body": json.dumps({"execution": name, "status": resp["StatusCode"]})}
    except _lambda.exceptions.DurableExecutionAlreadyStartedException:
        return {"statusCode": 200, "body": json.dumps({"execution": name, "status": "already running"})}
