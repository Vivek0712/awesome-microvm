"""Security review agent: the microVM half of the durable-function handoff.

A Lambda durable function *leases* this VM for one pull request. It creates a
callback, then launches this image with

    runHookPayload = {"lease": {"kind": "durable", "token": "<callback id>", "region": "us-east-1",
                                "heartbeat_s": 30, "id": "<execution name>"},
                      "task":  {"provider": "github" | "codecommit",
                                "repo": "owner/name" | "repo-name",
                                "pr": 42, "base": "<sha>", "head": "<sha>",
                                "clone_url": "https://...", "post": true}}

The hook runtime (microvm_hooks.py) owns the lease: /run answers 200 at once, `review`
runs in a daemon thread, a heartbeat thread calls SendDurableExecutionCallbackHeartbeat
every heartbeat_s seconds, the return value goes out as SendDurableExecutionCallbackSuccess,
and a raised LeaseError goes out as SendDurableExecutionCallbackFailure with its typed
ErrorType (CloneFailed, ScanFailed, PostFailed, BadPayload) so the orchestrator can decide
whether to relaunch. If the orchestrator gave up (heartbeat gets CallbackTimeoutException)
`lease.check()` raises between stages: nobody is waiting, stop spending. A /terminate
mid-review sends a retryable `Terminated` failure.

GET /status (built in) reports phase, elapsed time, heartbeats, findings so far, and the
log tail through the authenticated endpoint. POST /review runs the same pipeline
synchronously without a lease, for `mvm call` demos and CI.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

from microvm_hooks import HookApp, LeaseError

app = HookApp()

WORK_DIR = "/tmp/review"
REPORT_BUCKET = os.environ.get("REPORT_BUCKET")            # optional: full report to S3
MODEL_ID = os.environ.get("REVIEW_MODEL_ID")               # optional: Bedrock summary
GITHUB_TOKEN_SECRET_ARN = os.environ.get("GITHUB_TOKEN_SECRET_ARN")
MAX_FINDINGS_IN_RESULT = 50

SECRET_PATTERNS = [
    ("aws-access-key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private-key", re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("generic-secret", re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token)\b\s*[:=]\s*['\"][^'\"]{8,}['\"]")),
]


SHA_RX = re.compile(r"^[0-9a-fA-F]{7,40}$")
REPO_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(/[A-Za-z0-9][A-Za-z0-9_.-]*)?$")       # owner/name or a CodeCommit repo name

ReviewError = LeaseError                       # typed, structured failures; the runtime delivers them


# ----------------------------------------------------------------- lifecycle hooks
@app.on_ready
def ready(_ctx):
    subprocess.run(["git", "--version"], capture_output=True, check=True)
    subprocess.run(["python3.12", "-m", "bandit", "--version"], capture_output=True, check=True)
    return True                                  # snapshot: toolchain warm, nothing per-PR in memory


@app.on_validate
def validate(_ctx):
    # Exercise the scanner once so Lambda prefetches those snapshot pages on every launch.
    os.makedirs("/tmp/validate", exist_ok=True)
    with open("/tmp/validate/x.py", "w") as f:
        f.write("import subprocess\nsubprocess.call('ls', shell=True)\n")
    _run_bandit("/tmp/validate")


@app.on_lease
def review(task: dict, lease) -> dict:
    """The leased path: the runtime heartbeats, completes, and fails the callback for us."""
    return _review(task, lease)


# ----------------------------------------------------------------- the review itself
def _sh(cmd: list[str], cwd: str = "/tmp", timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)


def _clone_url(task: dict) -> str:
    if task.get("clone_url"):
        return task["clone_url"]
    if task.get("provider") == "codecommit":
        region = task.get("region") or os.environ.get("AWS_REGION", "us-east-1")
        return f"https://git-codecommit.{region}.amazonaws.com/v1/repos/{task['repo']}"
    return f"https://github.com/{task['repo']}.git"


def _github_token() -> str | None:
    if not GITHUB_TOKEN_SECRET_ARN:
        return None
    import boto3
    return boto3.client("secretsmanager").get_secret_value(SecretId=GITHUB_TOKEN_SECRET_ARN)["SecretString"]


def _validate(task: dict) -> None:
    """Task fields end up as git argv: reject anything that could be parsed as an option."""
    for key in ("repo", "pr", "head"):
        if key not in task:
            raise ReviewError("BadPayload", f"task is missing '{key}'")
    if not REPO_RX.match(str(task["repo"])):
        raise ReviewError("BadPayload", "repo must look like owner/name")
    for key in ("base", "head"):
        if task.get(key) and not SHA_RX.match(str(task[key])):
            raise ReviewError("BadPayload", f"{key} must be a commit sha")
    if task.get("clone_url") and not str(task["clone_url"]).startswith("https://"):
        raise ReviewError("BadPayload", "clone_url must be https")


def _clone(task: dict) -> str:
    app.job.phase("cloning")
    subprocess.run(["rm", "-rf", WORK_DIR], check=False)
    url = _clone_url(task)
    if task.get("provider") == "codecommit":
        _sh(["git", "config", "--global", "credential.helper", "!aws codecommit credential-helper $@"])
        _sh(["git", "config", "--global", "credential.UseHttpPath", "true"])
    elif task.get("provider", "github") == "github" and url.startswith("https://github.com/"):
        token = _github_token()
        if token:
            url = url.replace("https://github.com/", f"https://x-access-token:{token}@github.com/")
    proc = _sh(["git", "clone", "--no-checkout", "--filter=blob:none", "--", url, WORK_DIR], timeout=180)
    if proc.returncode != 0:
        raise ReviewError("CloneFailed", proc.stderr[-800:].replace(url, "<url>"), retryable=True)
    for sha in (task.get("base"), task.get("head")):
        if sha:
            proc = _sh(["git", "fetch", "--depth=1", "origin", sha], cwd=WORK_DIR, timeout=180)
            if proc.returncode != 0:
                raise ReviewError("CloneFailed", f"fetch {sha[:12]}: {proc.stderr[-400:]}", retryable=True)
    proc = _sh(["git", "checkout", "--quiet", task["head"], "--"], cwd=WORK_DIR)
    if proc.returncode != 0:
        raise ReviewError("CloneFailed", f"checkout: {proc.stderr[-400:]}", retryable=True)
    return WORK_DIR


def _diff(task: dict) -> tuple[str, list[str]]:
    app.job.phase("diffing")
    rng = f"{task['base']}..{task['head']}" if task.get("base") else task["head"]
    names = _sh(["git", "diff", "--name-only", rng, "--"], cwd=WORK_DIR)
    text = _sh(["git", "diff", rng, "--"], cwd=WORK_DIR)
    if names.returncode != 0 or text.returncode != 0:
        # never report "no findings" for a diff that was never produced
        raise ReviewError("ScanFailed", f"git diff {rng[:25]}: {(names.stderr or text.stderr)[-400:]}")
    return text.stdout[:400_000], names.stdout.split()


def _run_bandit(path: str, files: list[str] | None = None) -> list[dict]:
    targets = [f for f in (files or []) if f.endswith(".py") and os.path.exists(os.path.join(path, f))]
    if files is not None and not targets:
        return []
    cmd = ["python3.12", "-m", "bandit", "-q", "-f", "json"] + (targets if targets else ["-r", "."])
    proc = _sh(cmd, cwd=path, timeout=240)
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        raise ReviewError("ScanFailed", f"bandit produced no JSON: {proc.stderr[-500:]}") from None
    return [{"tool": "bandit", "severity": r["issue_severity"], "confidence": r["issue_confidence"],
             "file": r["filename"], "line": r["line_number"], "id": r["test_id"], "text": r["issue_text"]}
            for r in data.get("results", [])]


def _scan_secrets(diff: str) -> list[dict]:
    """Report where a secret-shaped string was added, never the string: the report is
    posted on the pull request, and a scanner that reprints a key is a second leak."""
    out, current, lineno = [], "?", 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("@@"):
            try:
                lineno = int(line.split("+", 1)[1].split(",")[0].split(" ")[0]) - 1
            except (IndexError, ValueError):
                lineno = 0
        elif line.startswith("+") and not line.startswith("+++"):
            lineno += 1
            for name, rx in SECRET_PATTERNS:
                if rx.search(line):
                    out.append({"tool": "secret-scan", "severity": "HIGH", "confidence": "MEDIUM",
                                "file": current, "line": lineno, "id": name,
                                "text": f"added line matches the {name} pattern (value redacted)"})
        elif not line.startswith("-"):
            lineno += 1
    return out


def _summarize(findings: list[dict], diff: str) -> str | None:
    if not MODEL_ID:
        return None
    app.job.phase("summarizing")
    import boto3
    prompt = ("You are a senior security reviewer. Summarize the risk of this pull request in five short "
              "bullet points, citing the findings below. Be specific and concise.\n\nFindings:\n"
              + json.dumps(findings[:30], indent=1) + "\n\nDiff (truncated):\n" + diff[:30_000])
    resp = boto3.client("bedrock-runtime").converse(
        modelId=MODEL_ID, messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 600})
    return resp["output"]["message"]["content"][0]["text"]


def _report(task: dict, findings: list[dict], files: list[str], summary: str | None) -> str:
    sev = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for f in findings:
        sev[f["severity"]] = sev.get(f["severity"], 0) + 1
    lines = [f"## Security review of `{task['head'][:12]}`",
             f"{len(files)} file(s) changed. Findings: **{sev['HIGH']} high**, {sev['MEDIUM']} medium, {sev['LOW']} low.", ""]
    if summary:
        lines += [summary, ""]
    for f in sorted(findings, key=lambda x: ("HIGH", "MEDIUM", "LOW").index(x["severity"]))[:MAX_FINDINGS_IN_RESULT]:
        loc = f"{f['file']}:{f['line']}" if f.get("line") else f["file"]
        lines.append(f"- **{f['severity']}** `{f['id']}` in `{loc}`: {f['text']}")
    if not findings:
        lines.append("No findings from bandit or the secret scan.")
    lines += ["", f"_Reviewed inside a Lambda MicroVM `{app.microvm_id or '?'}` leased by a durable function._"]
    return "\n".join(lines)


def _post(task: dict, body: str) -> dict:
    app.job.phase("posting")
    if task.get("provider") == "codecommit":
        import boto3
        cc = boto3.client("codecommit", region_name=task.get("region") or os.environ.get("AWS_REGION"))
        resp = cc.post_comment_for_pull_request(
            pullRequestId=str(task["pr"]), repositoryName=task["repo"],
            beforeCommitId=task["base"], afterCommitId=task["head"], content=body[:10_000])
        return {"provider": "codecommit", "comment_id": resp["comment"]["commentId"]}
    token = _github_token()
    if not token:
        raise ReviewError("PostFailed", "GITHUB_TOKEN_SECRET_ARN is not set on the image; cannot post to GitHub")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{task['repo']}/issues/{task['pr']}/comments",
        data=json.dumps({"body": body[:60_000]}).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "User-Agent": "microvm-durable-handoff"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return {"provider": "github", "comment_url": json.loads(r.read())["html_url"]}
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        raise ReviewError("PostFailed", f"GitHub comment failed: {e}", retryable=True) from None


def _review(task: dict, lease=None) -> dict:
    """The pipeline. With a lease, `lease.check()` between stages aborts once the orchestrator
    stopped waiting (do not post, do not spend); without one (POST /review) it is a no-op."""
    _validate(task)
    started = time.time()
    check = lease.check if lease else (lambda: None)

    _clone(task)
    check()
    diff, files = _diff(task)
    app.job.phase("scanning")
    findings = _run_bandit(WORK_DIR, files) + _scan_secrets(diff)
    app.job.counter("findings", len(findings))
    check()
    summary = _summarize(findings, diff) if findings else None
    report = _report(task, findings, files, summary)
    check()
    posted = _post(task, report) if task.get("post", True) else {"provider": "none"}
    app.job.phase("reported")
    report_key = None
    if REPORT_BUCKET:
        import boto3
        report_key = f"reviews/{task['repo'].replace('/', '_')}/{task['pr']}/{task['head'][:12]}.md"
        boto3.client("s3").put_object(Bucket=REPORT_BUCKET, Key=report_key, Body=report.encode())
    return {                                       # stays well under the 256 KB callback limit
        "status": "reviewed", "repo": task["repo"], "pr": task["pr"], "head": task["head"],
        "files_changed": len(files), "findings": len(findings),
        "high": sum(f["severity"] == "HIGH" for f in findings),
        "top_findings": findings[:MAX_FINDINGS_IN_RESULT], "posted": posted,
        "report_s3_key": report_key, "duration_s": round(time.time() - started, 1),
        "microvm_id": app.microvm_id, "heartbeats": lease.heartbeats if lease else 0,
    }


# ----------------------------------------------------------------- routes
@app.route("POST", "/review")
def review_route(body, _headers):
    """Synchronous variant for demos and tests: no durable function, no callback."""
    try:
        return 200, _review(body)
    except ReviewError as e:
        return 422, {"error_type": e.error_type, "message": str(e), "retryable": e.retryable}


if __name__ == "__main__":
    app.serve(port=8080)
