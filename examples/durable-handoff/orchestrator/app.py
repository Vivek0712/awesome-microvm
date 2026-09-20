"""Durable orchestrator: lease a Lambda MicroVM to a security review, wait for its callback.

The pattern in one sentence: the durable function owns the lifecycle, the microVM
owns the work, and a single-use callback id is the lease that ties them together.

    create_callback  ->  FleetManager.lease(runHookPayload={lease, task})  ->  callback.result()
                                                                           ->  TerminateMicrovm

The lease itself is `microvm.integrations.durable.lease_with_relaunch`: an at-most-once
launch step with a clientToken derived from the callback id (a replay gets the same VM
back), the callback id inside runHookPayload (no polling for RUNNING, no dispatch call),
and time bounded twice (callback timeout = budget, maximumDurationInSeconds = budget +
slack). It terminates the VM in every branch it can see and never wraps
`callback.result()` in try/finally, because the SDK suspends by raising from it.

This file keeps what is specific to a review: turning a webhook or trigger into a task,
resolving a CodeCommit branch to its open pull request, the fan-out over
`microvm.integrations.durable.lease_map` (`{"mode": "fanout", "shards": [...]}`: the plane
sizes it from the memory quota and refuses before launching when it cannot fit), and the
deterministic execution name that makes redeliveries reattach.
"""

from __future__ import annotations

import os
import re

from aws_durable_execution_sdk_python import DurableContext, StepContext, durable_execution, durable_step

from microvm import FleetManager, LeasePolicy, PlaneConfig
from microvm.integrations.durable import lease_with_relaunch

try:  # microvm-ctl >= 0.3.0: the planned fan-out
    from microvm.integrations.durable import lease_map
except ImportError:  # pragma: no cover - older microvm-ctl: single leases only
    lease_map = None

IMAGE = os.environ["MVM_IMAGE"]                              # name or ARN of the agent image
IMAGE_VERSION = os.environ.get("MVM_IMAGE_VERSION") or None
BUDGET_S = int(os.environ.get("JOB_BUDGET_S", "900"))        # hard cap on one review
HEARTBEAT_TIMEOUT_S = int(os.environ.get("HEARTBEAT_TIMEOUT_S", "120"))
MAX_RELAUNCHES = int(os.environ.get("MAX_RELAUNCHES", "1"))
SLACK_S = 120                                                # VM outlives the callback by this much, no more
BASELINE_MIB = int(os.environ.get("BASELINE_MIB", "2048"))   # memory of one agent VM, for the fan-out plan
MAX_CONCURRENCY = int(os.environ["MAX_CONCURRENCY"]) if os.environ.get("MAX_CONCURRENCY") else None
POLICY = LeasePolicy(budget_s=BUDGET_S, heartbeat_timeout_s=HEARTBEAT_TIMEOUT_S, slack_s=SLACK_S)

_fm: FleetManager | None = None


def fleet_manager() -> FleetManager:
    """Built lazily inside steps: the constructor reads Service Quotas, a side effect
    that has no business running during replay outside a durable operation."""
    global _fm
    if _fm is None:
        _fm = FleetManager(PlaneConfig())        # MVM_REGION / MVM_EXECUTION_ROLE_ARN from the environment
    return _fm


class _LazyFleet:
    """What the library's steps receive: resolves to the real FleetManager on first use,
    so nothing touches AWS while the SDK replays the handler up to the next step."""

    def __getattr__(self, name):
        return getattr(fleet_manager(), name)


FM = _LazyFleet()


def review(context: DurableContext, task: dict, label: str = "review") -> dict:
    """One review on one leased VM, relaunched once on a retryable outcome. The outcome
    is the library's: {"status": "done" | "failed" | "timed_out", "result" | "error",
    "retryable", "vm", "attempt"}."""
    return lease_with_relaunch(context, FM, IMAGE, task, max_relaunches=MAX_RELAUNCHES, label=label,
                               policy=POLICY, version=IMAGE_VERSION)


# ----------------------------------------------------------------- event parsing (pure)
def task_from_event(event: dict) -> dict | None:
    """Accepts a normalized task, a GitHub pull_request webhook body, or a CodeCommit trigger."""
    if "task" in event:
        return event["task"]
    if "pull_request" in event:                                   # GitHub webhook (already verified upstream)
        if event.get("action") not in ("opened", "synchronize", "reopened"):
            return None
        pr = event["pull_request"]
        return {"provider": "github", "repo": event["repository"]["full_name"], "pr": pr["number"],
                "base": pr["base"]["sha"], "head": pr["head"]["sha"],
                "clone_url": event["repository"]["clone_url"], "post": True}
    if "Records" in event:                                        # CodeCommit repository trigger
        rec = event["Records"][0]
        if rec.get("eventName") == "TriggerEventTest":
            return None
        ref = rec["codecommit"]["references"][0]
        return {"provider": "codecommit", "repo": rec["eventSourceARN"].split(":")[-1],
                "branch": ref["ref"].removeprefix("refs/heads/"), "head": ref["commit"], "post": True}
    return None


def shard_tasks(event: dict) -> list[dict]:
    """The tasks of a fan-out event. Each element of `shards` is a task on its own; with a
    `task` in the event as well, every shard is merged into it (an object) or becomes its
    `paths` (anything else), which is how one review is split by path."""
    shards = event.get("shards") or []
    base = event.get("task")
    if not isinstance(base, dict):
        return [s for s in shards if isinstance(s, dict)]
    return [{**base, "post": False, **(s if isinstance(s, dict) else {"paths": s})} for s in shards]


@durable_step
def resolve_codecommit_pr(step: StepContext, task: dict) -> dict | None:
    """CodeCommit triggers carry a branch, not a PR: look the open PR up (a side effect, so a step)."""
    import boto3
    cc = boto3.client("codecommit")
    for pr_id in cc.list_pull_requests(repositoryName=task["repo"], pullRequestStatus="OPEN").get("pullRequestIds", []):
        pr = cc.get_pull_request(pullRequestId=pr_id)["pullRequest"]
        for target in pr["pullRequestTargets"]:
            if target["sourceReference"] == f"refs/heads/{task['branch']}":
                return {**task, "pr": pr_id, "base": target["destinationCommit"]}
    return None


# ----------------------------------------------------------------- handlers
@durable_execution
def handler(event: dict, context: DurableContext) -> dict:
    if event.get("mode") == "fanout":
        return fanout(context, shard_tasks(event))
    task = task_from_event(event)
    if not task:
        return {"status": "skipped", "reason": "not a reviewable event"}
    if task.get("provider") == "codecommit" and "pr" not in task:
        task = context.step(resolve_codecommit_pr(task), name="resolve-pr")
        if not task:
            return {"status": "skipped", "reason": "no open pull request for that branch"}
    return review(context, task)


def fanout(context: DurableContext, tasks: list[dict]) -> dict:
    """One VM per shard, each with its own lease, under the plane's plan: `lease_map` runs a
    `shard-plan` step (the memory quota read once, the honest concurrency, a refusal before
    anything launches), then `context.map` with one `lease_with_relaunch` per shard, at most
    the plan's concurrency (or MAX_CONCURRENCY when lower) at a time. Returns its dict:
    {"status": "done" | "rejected" | "approval_required", "plan", "outcomes", "succeeded", "failed"}."""
    if not tasks:
        return {"status": "skipped", "reason": "no shards"}
    if lease_map is None:
        return {"status": "unsupported", "reason": "fan-out needs microvm-ctl >= 0.3.0 (lease_map)"}
    return lease_map(context, FM, IMAGE, tasks, policy=POLICY, label="shard", baseline_mib=BASELINE_MIB,
                     max_concurrency=MAX_CONCURRENCY, max_relaunches=MAX_RELAUNCHES, version=IMAGE_VERSION)


def execution_name(task: dict) -> str:
    """Deterministic name so duplicate webhook deliveries reattach to the same execution."""
    raw = f"{task.get('repo')}-{task.get('pr')}-{str(task.get('head'))[:12]}"
    return re.sub(r"[^a-zA-Z0-9_-]", "-", raw)[:64]
