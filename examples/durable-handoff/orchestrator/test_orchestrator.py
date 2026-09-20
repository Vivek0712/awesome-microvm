"""Run the durable orchestrator locally, end to end, with the AWS testing SDK.

    python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt aws-durable-execution-sdk-python-testing pytest
    .venv/bin/pytest -q test_orchestrator.py

FleetManager is faked, so no AWS account is touched. The first three tests are the three
paths the lease can take: success, a typed retryable failure followed by a fatal one,
and a callback that is never completed (heartbeat/timeout, relaunch once, give up).
The outcome shape is the library's (`microvm.integrations.durable`): `status` is
"done", "failed", or "timed_out", with `result` or a typed `error`, plus `attempt`.
The fan-out tests drive `lease_map` through the fake's `plan` (real arithmetic on a fake
8 GB quota): two shards complete, and a plan that cannot fit launches nothing.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

os.environ.setdefault("MVM_IMAGE", "security-review-agent")
os.environ.setdefault("MVM_REGION", "us-east-1")
os.environ.setdefault("MVM_EXECUTION_ROLE_ARN", "arn:aws:iam::123456789012:role/agent")
os.environ["JOB_BUDGET_S"] = "4"
os.environ["HEARTBEAT_TIMEOUT_S"] = "2"
os.environ["MAX_RELAUNCHES"] = "1"
sys.path.insert(0, os.path.dirname(__file__))

import app  # noqa: E402
from aws_durable_execution_sdk_python.execution import ErrorObject  # noqa: E402
from aws_durable_execution_sdk_python_testing import DurableFunctionTestRunner  # noqa: E402
from microvm import LeasePolicy  # noqa: E402

TASK = {"provider": "github", "repo": "o/r", "pr": 7, "base": "b" * 40, "head": "h" * 40, "post": False}


class _Vm:
    def __init__(self, i):
        self.microvm_id, self.endpoint = f"microvm-fake-{i}", f"fake{i}.lambda-microvm.us-east-1.on.aws"


class _Exceptions:
    class ResourceNotFoundException(Exception):
        pass


class FakeFleetManager:
    api = type("Api", (), {"exceptions": _Exceptions})()
    quota_gb = 8.0            # the fake account's memory quota: four 2 GB VMs at once
    launch_rate = 0.8

    def __init__(self):
        self.leases, self.terminated, self.plans = [], [], []

    def plan(self, shards, baseline_mib, policy=None):
        """The real plan arithmetic (microvm.lease.plan_fanout) on the fake quota."""
        from microvm.lease import LeasePolicy, plan_fanout
        self.plans.append({"shards": shards, "baseline_mib": baseline_mib, "policy": policy})
        return plan_fanout(shards, baseline_mib, policy or LeasePolicy(),
                           memory_quota_gb=self.quota_gb, launch_rate=self.launch_rate)

    def lease(self, image, lease, task, policy=None, *, version=None, execution_role=None,
              ingress=None, egress=None):
        lease.validate()
        self.leases.append({"image": image, "lease": lease, "task": task, "policy": policy, "version": version})
        return _Vm(len(self.leases))

    def terminate(self, microvm_id):
        self.terminated.append(microvm_id)


@pytest.fixture()
def fm():
    fake = FakeFleetManager()
    app._fm = fake
    yield fake
    app._fm = None


def _outcome(res):
    return json.loads(res.result) if isinstance(res.result, str) else res.result


def test_lease_success_terminates_once(fm):
    runner = DurableFunctionTestRunner(handler=app.handler)
    with runner:
        arn = runner.run_async({"task": TASK})
        cb = runner.wait_for_callback(arn, timeout=30)
        launch = fm.leases[0]
        assert launch["image"] == "security-review-agent" and launch["task"]["pr"] == 7
        assert launch["lease"].kind == "durable" and launch["lease"].token == cb
        assert launch["policy"].budget_s == 4 and launch["policy"].max_duration() == 4 + app.SLACK_S
        runner.send_callback_heartbeat(cb)
        completion = {"microvm_id": "microvm-fake-1", "lease_id": launch["lease"].id, "elapsed_s": 1.5,
                      "result": {"status": "reviewed", "findings": 2}}
        runner.send_callback_success(cb, json.dumps(completion).encode())
        out = _outcome(runner.wait_for_result(arn, timeout=30))
    assert out["status"] == "done" and out["attempt"] == 0 and out["result"]["findings"] == 2
    assert out["vm"]["microvm_id"] == "microvm-fake-1"
    assert fm.terminated == ["microvm-fake-1"]


def test_retryable_failure_relaunches_once_then_fatal(fm):
    runner = DurableFunctionTestRunner(handler=app.handler)
    with runner:
        arn = runner.run_async({"task": TASK})
        cb1 = runner.wait_for_callback(arn, name="review-0-callback", timeout=30)
        runner.send_callback_failure(cb1, ErrorObject(
            message="clone timed out", type="CloneFailed",
            data=json.dumps({"error": {"error_type": "CloneFailed", "message": "clone timed out",
                                       "retryable": True, "data": {}}}), stack_trace=None))
        cb2 = runner.wait_for_callback(arn, name="review-1-callback", timeout=30)
        assert cb2 != cb1
        runner.send_callback_failure(cb2, ErrorObject(
            message="bandit crashed", type="ScanFailed",
            data=json.dumps({"error": {"error_type": "ScanFailed", "message": "bandit crashed",
                                       "retryable": False, "data": {}}}), stack_trace=None))
        out = _outcome(runner.wait_for_result(arn, timeout=30))
    assert out["status"] == "failed" and out["attempt"] == 1 and out["retryable"] is False
    assert out["error"]["error_type"] == "ScanFailed" and out["error"]["message"] == "bandit crashed"
    assert fm.terminated == ["microvm-fake-1", "microvm-fake-2"]


def test_silent_agent_times_out_relaunches_then_gives_up(fm):
    runner = DurableFunctionTestRunner(handler=app.handler)
    with runner:
        arn = runner.run_async({"task": TASK})
        runner.wait_for_callback(arn, name="review-0-callback", timeout=30)   # never answered
        out = _outcome(runner.wait_for_result(arn, timeout=60))
    assert out["status"] == "timed_out" and out["attempt"] == 1 and out["retryable"] is True
    assert out["error"]["error_type"] == "CallbackTimeout"
    assert len(fm.leases) == 2 and fm.terminated == ["microvm-fake-1", "microvm-fake-2"]


needs_lease_map = pytest.mark.skipif(app.lease_map is None, reason="microvm-ctl without lease_map (< 0.3.0)")


@needs_lease_map
def test_fanout_two_shards_plan_then_map(fm, monkeypatch):
    # both shards wait at once; a 2 s heartbeat timeout is for the silent-agent test, not for this one
    monkeypatch.setattr(app, "POLICY", LeasePolicy(budget_s=30, heartbeat_timeout_s=20, slack_s=app.SLACK_S))
    runner = DurableFunctionTestRunner(handler=app.handler)
    with runner:
        arn = runner.run_async({"mode": "fanout", "shards": [TASK, {**TASK, "pr": 8}]})
        # lease_map labels shard i "shard-i", lease_with_relaunch adds the attempt: "shard-<i>-0-callback"
        callbacks = {name: runner.wait_for_callback(arn, name=name, timeout=30)
                     for name in ("shard-0-0-callback", "shard-1-0-callback")}
        for cb in callbacks.values():
            launch = next(x for x in fm.leases if x["lease"].token == cb)
            completion = {"microvm_id": f"microvm-fake-{fm.leases.index(launch) + 1}",
                          "lease_id": launch["lease"].id, "elapsed_s": 2.0,
                          "result": {"status": "reviewed", "pr": launch["task"]["pr"]}}
            runner.send_callback_success(cb, json.dumps(completion).encode())
        out = _outcome(runner.wait_for_result(arn, timeout=60))
    assert len(set(callbacks.values())) == 2
    assert fm.plans == [{"shards": 2, "baseline_mib": 2048, "policy": app.POLICY}]
    assert out["status"] == "done" and out["succeeded"] == 2 and out["failed"] == 0 and out["errors"] == []
    assert out["plan"]["concurrency"] == 2 and out["plan"]["rejected"] is None
    assert sorted(o["result"]["pr"] for o in out["outcomes"]) == [7, 8]
    assert all(o["status"] == "done" and o["attempt"] == 0 for o in out["outcomes"])
    assert sorted(x["task"]["pr"] for x in fm.leases) == [7, 8]
    assert sorted(fm.terminated) == ["microvm-fake-1", "microvm-fake-2"]


@needs_lease_map
def test_fanout_rejected_plan_launches_nothing(fm):
    fm.quota_gb = 1.0                                             # a 2 GB baseline cannot fit at all
    runner = DurableFunctionTestRunner(handler=app.handler)
    with runner:
        arn = runner.run_async({"mode": "fanout", "shards": [TASK] * 3})
        out = _outcome(runner.wait_for_result(arn, timeout=30))
    assert out["status"] == "rejected" and "exceeds the memory quota" in out["reason"]
    assert fm.plans[0]["shards"] == 3 and fm.leases == [] and fm.terminated == []


def test_event_parsing_is_pure():
    gh = {"action": "synchronize", "pull_request": {"number": 3, "base": {"sha": "a" * 40}, "head": {"sha": "b" * 40}},
          "repository": {"full_name": "o/r", "clone_url": "https://github.com/o/r.git"}}
    assert app.task_from_event(gh)["pr"] == 3
    assert app.task_from_event({**gh, "action": "closed"}) is None
    cc = {"Records": [{"eventSourceARN": "arn:aws:codecommit:us-east-1:1:repo", "eventName": "TriggerEventTest",
                       "codecommit": {"references": [{"ref": "refs/heads/x", "commit": "c" * 40}]}}]}
    assert app.task_from_event(cc) is None
    assert app.execution_name({"repo": "o/r", "pr": 3, "head": "b" * 40}) == "o-r-3-bbbbbbbbbbbb"
    assert app.shard_tasks({"mode": "fanout", "shards": [TASK, TASK]}) == [TASK, TASK]
    split = app.shard_tasks({"mode": "fanout", "task": TASK, "shards": [["src/"], {"pr": 9}]})
    assert split[0] == {**TASK, "post": False, "paths": ["src/"]} and split[1]["pr"] == 9
    assert app.shard_tasks({"mode": "fanout"}) == []
