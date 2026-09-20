"""Benchmark the lease handoff: how long from "start" to the VM working, from the VM's
completion to the orchestrator resuming, end to end, and what one lease costs.

    MVM_PROFILE=me python3 benchmarks/handoff_bench.py --runs 3 --kinds sfn,durable,sqs,eventbridge,http \
        --sfn-arn arn:aws:states:...:stateMachine:microvm-sfn-handoff-lease \
        --durable-function microvm-durable-handoff-orchestrator:live \
        --queue-url https://sqs.../microvm-lease-sqs --bus microvm-lease \
        --http-target https://<collector>.lambda-url.us-east-1.on.aws/ \
        --collector-queue-url https://sqs.../microvm-lease-collector
    python3 benchmarks/handoff_bench.py --dry-run --kinds sfn,sqs        # the plan, no AWS

    # leases at scale: fan-outs of 4 and 8 shards on a 512 MiB build of the agent, in-VM parallel steps,
    # and the pre-flight refusal drill (nothing launched)
    python3 benchmarks/handoff_bench.py --fanout 4,8 --fanout-kinds sfn,durable \
        --fanout-image handoff-agent-small --baseline-mib 512 \
        --sfn-map-arn arn:aws:states:...:stateMachine:microvm-sfn-handoff-map-map \
        --durable-map-function microvm-durable-handoff-small-orchestrator:live
    python3 benchmarks/handoff_bench.py --in-vm-parallel --refusal-drill
    python3 benchmarks/handoff_bench.py --summarize benchmarks/results/handoff-*.json

    # rewrite earlier result files with the services' own timestamps (read-only APIs, nothing launched)
    python3 benchmarks/handoff_bench.py --backfill benchmarks/results/handoff-*.json \
        --sfn-arn ... --sfn-map-arn ... --durable-function ... --durable-map-function ...

Every kind leases the same image (examples/handoff-agent) with the task
{"steps": ["echo bench", "sleep 3"]}. The bench watches from the outside only: the fleet
listing every 0.5 s (when the VM appears and turns RUNNING), the VM's GET /status (when the
lease was accepted and when it finished), and the orchestrator's own terminal state (Step
Functions describe-execution, Lambda get-durable-execution, or the queue message the generic
controller would read). One VM at a time, a settle loop for TERMINATED between runs, and
everything the bench launched is terminated on exit. Emits JSON plus an SVG transcript to
benchmarks/results/handoff-<timestamp>.json/.svg like capture_demos.py.

The numbers come from the services' clocks, never from when the bench's poll loop happened to
notice (that loop mints a token per VM and settles between runs; it is the bench's latency, not
the service's). `end_to_end_s` for sfn is describe-execution stopDate - startDate, for durable
get-durable-execution EndTimestamp - StartTimestamp (`orchestrator_started_at` /
`orchestrator_stopped_at` in the record); `completion_to_resume_s` is that stop time minus the
VM's completion, taken from the VM's own payload (`/status.started`, 1 s resolution, plus the
`elapsed_s` in the completion the orchestrator received) or else the last /status that showed
lease.done, clamped at 0; VM-seconds run from GetMicrovm's startedAt to its terminatedAt, else
to the first TERMINATING or TERMINATED the bench saw, capped at budget + slack. Where the bench's
own observation had to stand in (the generic kinds' message arrival, which includes the poll
wait; a service field missing) the record says so in `notes` and `sources`.

`--durable-function` must be a durable-handoff orchestrator deployed with
ImageName=handoff-agent: its event `{"task": {...}}` passes the task straight to the lease.

Fan-outs (`--fanout N[,M] --fanout-kinds sfn,durable`) start the Map state machine
(`--sfn-map-arn`, template-map.yaml) with `{"shards": [task x N]}` or invoke the durable
orchestrator (`--durable-map-function`, deployed with ImageName=--fanout-image) with
`{"mode": "fanout", "shards": [task x N]}`, then watch the whole image: when every one of the N
members has been RUNNING, when the first and the slowest shard report `lease.done`, when the
orchestrator ends, and the VM-seconds every member accrued. `ok` means the orchestrator
SUCCEEDED and its output carries N shards (sfn: N payloads in the output array; durable:
`succeeded == N`); "shards reported done" from /status stays informational because the
orchestrator's own Terminate step races that poll. One fan-out at a time, the same 120 s settle.
`--in-vm-parallel` leases the 2 GB agent twice with four `sleep 3` steps, sequential then
`"parallel": true`, and reports each VM's elapsed_s. `--refusal-drill` asks the plane to size
40 x 2 GB, prints the plan, and asserts the launch was refused before a single RunMicrovm went out.

`--backfill RESULT_JSON...` rewrites earlier result files in place with those service timestamps:
executions are found by their recorded names (list_executions on --sfn-arn / --sfn-map-arn,
list_durable_executions_by_function with DurableExecutionName on --durable-function /
--durable-map-function), members through GetMicrovm; one before/after line per run. It launches
nothing. `--summarize` reads old and backfilled files alike.
"""

from __future__ import annotations

import argparse
import calendar
import concurrent.futures as futures
import json
import secrets
import statistics
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from rich.console import Console
from rich.table import Table

RESULTS = Path(__file__).parent / "results"
KINDS = ("sfn", "durable", "sqs", "eventbridge", "http")
GENERIC = ("sqs", "eventbridge", "http")
FANOUT_KINDS = ("sfn", "durable")
TASK = {"steps": ["echo bench", "sleep 3"]}
PARALLEL_STEPS = ["sleep 3"] * 4
DRILL_SHARDS, DRILL_BASELINE_MIB = 40, 2048
POLL_S = 0.5
SETTLE_S = 120
STATUS_TIMEOUT_S = 3          # one /status poll must not stall the loop when the VM is already gone
# published Lambda MicroVM rates for the 2 GB tier: memory per GB-second plus two vCPU-seconds
VM_USD_PER_S = 0.0000276944 + 2 * 0.0000036667
# the same two rates per GB-second of any tier (vCPU = memory / 2): the 2 GB figure above is 2 x this
VM_USD_PER_GB_S = 0.0000276944 / 2 + 0.0000036667
# what the orchestrator itself bills per lease: 4 Standard transitions, 3 durable operations, 0 for a poller
ORCHESTRATOR_USD = {"sfn": 4 * 0.000025, "durable": 3 * 0.000008, "sqs": 0.0, "eventbridge": 0.0, "http": 0.0}
REQUIRED = {
    "sfn": ["sfn_arn"], "durable": ["durable_function"], "sqs": ["queue_url"],
    "eventbridge": ["bus", "collector_queue_url"], "http": ["http_target", "collector_queue_url"],
}
REQUIRED_FANOUT = {"sfn": ["sfn_map_arn"], "durable": ["durable_map_function"]}
TERMINAL_SFN = {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"}
TERMINAL_DURABLE = {"SUCCEEDED", "FAILED", "TIMED_OUT", "STOPPED"}
GENERIC_NOTE = ("end_to_end_s is the message's arrival as seen by the bench's receive loop "
                "(includes the poll wait)")


def pctile(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else None


def iso_utc_to_epoch(s: str) -> float:
    """The hook runtime's `started` is "%Y-%m-%dT%H:%M:%SZ" (second resolution)."""
    return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))


def _epoch(ts) -> float | None:
    """boto3 hands timestamps back as datetimes; epoch floats pass through."""
    if ts is None:
        return None
    if hasattr(ts, "timestamp"):
        return float(ts.timestamp())
    return float(ts)


def _json(blob):
    """A JSON field that may arrive as str, bytes, or already parsed (or not at all)."""
    if isinstance(blob, (bytes, bytearray)):
        blob = blob.decode("utf-8", "replace")
    if isinstance(blob, str):
        try:
            return json.loads(blob)
        except ValueError:
            return None
    return blob


def _round(m: dict) -> dict:
    """Seconds to the millisecond, dollars to the micro-dollar (a 512 MiB shard is ~$0.00001 a second)."""
    return {k: (round(v, 6 if k == "cost_usd" else 3) if isinstance(v, float) else v) for k, v in m.items()}


def _note(rec, text: str) -> None:
    """Append once: metrics() runs more than once per record."""
    if text not in rec.notes:
        rec.notes.append(text)


def _result_from_snapshot(snap: dict) -> dict | None:
    """The agent logs its result on the last line ("passed n step(s)", result={...})."""
    for line in reversed(snap.get("log_tail") or []):
        if isinstance(line, dict) and isinstance(line.get("result"), dict):
            return line["result"]
    return None


def _elapsed_from(output) -> float | None:
    """The VM's own `elapsed_s` in the completion payload an orchestrator hands back as its output
    (sfn: the payload itself; durable: the envelope is stripped, so usually nothing)."""
    if isinstance(output, dict):
        for candidate in (output, output.get("result")):
            if isinstance(candidate, dict) and isinstance(candidate.get("elapsed_s"), (int, float)):
                return float(candidate["elapsed_s"])
    return None


def vm_span(started_at, t_running, terminated_at, seen: dict, max_duration: int, now: float | None = None):
    """VM-seconds and how they were obtained: the plane's startedAt -> terminatedAt when GetMicrovm has
    them, else the bench's first sighting of RUNNING and of TERMINATING/TERMINATED (whichever came
    first), else "now" for a VM still live; never negative, capped at budget + slack."""
    start, start_how = (started_at, "startedAt") if started_at else (t_running, "RUNNING seen")
    if not start:
        return None, None
    if terminated_at:
        end, end_how = terminated_at, "terminatedAt"
    else:
        observed = [(t, f"first {state} seen") for state, t in seen.items() if t]
        end, end_how = min(observed) if observed else ((now or time.time()), "now (still live)")
    secs, how = max(end - start, 0.0), f"{start_how} -> {end_how}"
    if secs > max_duration:
        secs, how = float(max_duration), how + ", capped at budget + slack"
    return secs, how


@dataclass
class Run:
    kind: str
    index: int
    name: str
    t0: float = 0.0
    vm_id: str | None = None
    t_seen: float | None = None
    t_running: float | None = None
    t_lease: float | None = None          # VM clock: job `started` once the lease shows in /status (1 s res.)
    work_s: float | None = None           # VM clock: /status elapsed_s when lease.done was first seen
    t_vm_done: float | None = None        # bench clock: when /status first showed lease.done
    vm_elapsed_s: float | None = None     # VM clock: elapsed_s in the completion payload the orchestrator got
    t_orch_done: float | None = None      # bench clock: when the bench saw the terminal state / the message
    orchestrator_started_at: float | None = None   # service clock: sfn startDate, durable StartTimestamp
    orchestrator_stopped_at: float | None = None   # service clock: sfn stopDate, durable EndTimestamp
    t_terminating: float | None = None    # bench clock: first TERMINATING seen
    t_terminated: float | None = None     # bench clock: first TERMINATED seen
    started_at: float | None = None       # plane clock: GetMicrovm startedAt
    terminated_at: float | None = None    # plane clock: GetMicrovm terminatedAt
    orchestrator_status: str | None = None
    vm_error: dict | None = None
    sources: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def orchestrator_span(self):
        """(start, end, how): the service's clock for sfn/durable, message arrival for the generic kinds."""
        if self.kind in GENERIC:
            return self.t0, self.t_orch_done, "message arrival (includes the poll wait)"
        if self.orchestrator_started_at and self.orchestrator_stopped_at:
            return self.orchestrator_started_at, self.orchestrator_stopped_at, "service start/stop"
        return self.t0, self.t_orch_done, "bench observation"

    def vm_done(self):
        """When the VM finished, by its own clock if the payload allows, else the bench's last /status."""
        if self.t_lease is not None and self.vm_elapsed_s is not None:
            return self.t_lease + self.vm_elapsed_s, "/status.started + payload elapsed_s"
        if self.t_vm_done is not None:
            return self.t_vm_done, "last /status with lease.done"
        return None, None

    def metrics(self, max_duration: int) -> dict:
        start, end, how = self.orchestrator_span()
        if end and self.kind not in GENERIC and how == "bench observation":
            _note(self, "orchestrator start/stop missing from the service record; bench observation used")
        if end and self.kind in GENERIC:
            _note(self, GENERIC_NOTE)
        vm_done, done_how = self.vm_done()
        c2r = None
        if end and vm_done is not None:
            c2r = end - vm_done
            if c2r < 0:
                _note(self, f"completion_to_resume_s clamped from {c2r:.3f} to 0 "
                            "(/status.started has 1 s resolution)")
                c2r = 0.0
        vm_seconds, vm_how = vm_span(self.started_at, self.t_running, self.terminated_at,
                                     {"TERMINATING": self.t_terminating, "TERMINATED": self.t_terminated},
                                     max_duration)
        self.sources = {"end_to_end": how if end else None, "vm_done": done_how, "vm_seconds": vm_how}
        m = {
            "launch_to_lease_s": self.t_lease - self.t0 if self.t_lease else None,
            "work_s": self.work_s,
            "completion_to_resume_s": c2r,
            "end_to_end_s": end - start if end else None,
            "vm_seconds": vm_seconds,
            "cost_usd": (vm_seconds * VM_USD_PER_S + ORCHESTRATOR_USD[self.kind]) if vm_seconds else None,
        }
        return _round(m)


@dataclass
class Member:
    """One VM of a fan-out, as seen from the fleet listing, its /status, and GetMicrovm afterwards."""
    vm_id: str
    t_seen: float
    endpoint: str | None = None
    started_at: float | None = None       # plane clock: ListMicrovms / GetMicrovm startedAt
    t_running: float | None = None
    t_done: float | None = None
    work_s: float | None = None
    t_terminating: float | None = None    # bench clock: first TERMINATING seen
    t_terminated: float | None = None     # bench clock: first TERMINATED seen
    terminated_at: float | None = None    # plane clock: GetMicrovm terminatedAt
    error: dict | None = None
    vm_seconds_how: str | None = None

    def vm_seconds(self, max_duration: int, now: float | None = None):
        secs, self.vm_seconds_how = vm_span(
            self.started_at, self.t_running, self.terminated_at,
            {"TERMINATING": self.t_terminating, "TERMINATED": self.t_terminated}, max_duration, now)
        return secs


@dataclass
class Fanout:
    kind: str
    shards: int
    name: str
    image: str
    baseline_mib: int
    t0: float = 0.0
    t_all_running: float | None = None
    t_orch_done: float | None = None      # bench clock: when the bench saw the terminal state
    orchestrator_started_at: float | None = None   # service clock
    orchestrator_stopped_at: float | None = None   # service clock
    orchestrator_status: str | None = None
    output_shards: int | None = None      # sfn: payloads in the output array; durable: result["succeeded"]
    output_status: str | None = None      # durable: result["status"]
    members: dict = field(default_factory=dict)      # vm_id -> Member
    sources: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def running_count(self) -> int:
        return sum(1 for m in self.members.values() if m.t_running)

    def shards_reported_done(self) -> int:
        return sum(1 for m in self.members.values() if m.t_done and not m.error)

    def orchestrator_span(self):
        if self.orchestrator_started_at and self.orchestrator_stopped_at:
            return self.orchestrator_started_at, self.orchestrator_stopped_at, "service start/stop"
        return self.t0, self.t_orch_done, "bench observation"

    def metrics(self, max_duration: int) -> dict:
        now = time.time()
        start, end, how = self.orchestrator_span()
        if end and how == "bench observation":
            _note(self, "orchestrator start/stop missing from the service record; bench observation used")
        done = sorted(m.t_done for m in self.members.values() if m.t_done)
        spans = [m.vm_seconds(max_duration, now) for m in self.members.values()]
        counted = [m for m, s in zip(self.members.values(), spans) if s is not None]
        vm_s = sum(s for s in spans if s is not None)
        self.sources = {"end_to_end": how if end else None,
                        "vm_seconds": dict(Counter(m.vm_seconds_how for m in counted))}
        gb = self.baseline_mib / 1024
        m = {
            "members_seen": len(self.members),
            "shards_done": len(done),          # informational: /status races the orchestrator's Terminate
            "shards_failed": sum(1 for x in self.members.values() if x.error),
            "shards_in_output": self.output_shards,
            "launch_to_all_running_s": self.t_all_running - self.t0 if self.t_all_running else None,
            "first_shard_done_s": done[0] - self.t0 if done else None,
            "slowest_shard_done_s": done[-1] - self.t0 if done else None,
            "end_to_end_s": end - start if end else None,
            "vm_seconds_total": vm_s if counted else None,
            "cost_usd": ((vm_s * gb * VM_USD_PER_GB_S + ORCHESTRATOR_USD[self.kind] * self.shards)
                         if counted else None),
        }
        return _round(m)

    def ok(self) -> bool:
        """The orchestrator's word: SUCCEEDED with N shards in its output (sfn: the array; durable:
        succeeded == N)."""
        return self.orchestrator_status == "SUCCEEDED" and self.output_shards == self.shards


@dataclass
class ParallelRun:
    """One 2 GB VM running four `sleep 3` steps, sequentially or with task.parallel."""
    mode: str
    name: str
    t0: float = 0.0
    vm_id: str | None = None
    t_running: float | None = None
    t_done: float | None = None
    elapsed_s: float | None = None        # VM clock: the job's elapsed_s when lease.done was first seen
    step_seconds: float | None = None     # sum of the steps' own duration_s from the VM's result
    passed: bool | None = None
    error: dict | None = None
    t_terminated: float | None = None
    notes: list = field(default_factory=list)

    def metrics(self, max_duration: int) -> dict:
        end = self.t_terminated or time.time()
        vm_seconds = min(end - self.t_running, max_duration) if self.t_running else None
        m = {
            "launch_to_running_s": self.t_running - self.t0 if self.t_running else None,
            "elapsed_s": self.elapsed_s,
            "step_seconds": self.step_seconds,
            "vm_seconds": vm_seconds,
            "cost_usd": vm_seconds * VM_USD_PER_S if vm_seconds else None,
        }
        return _round(m)


def apply_orchestrator(rec, d: dict) -> None:
    """Copy the orchestrator's own record (status, start, stop, output) onto a Run or Fanout."""
    rec.orchestrator_status = d["status"]
    rec.orchestrator_started_at, rec.orchestrator_stopped_at = d.get("started_at"), d.get("stopped_at")
    out = d.get("output")
    if isinstance(rec, Run):
        rec.vm_elapsed_s = _elapsed_from(out)
        if rec.vm_error is None and isinstance(out, dict) and isinstance(out.get("error"), dict):
            rec.vm_error = out["error"]
    elif rec.kind == "sfn":
        rec.output_shards = len(out) if isinstance(out, list) else None
    elif isinstance(out, dict):
        rec.output_status = out.get("status")
        rec.output_shards = out["succeeded"] if isinstance(out.get("succeeded"), int) else None


# ------------------------------------------------------------------------------- the services' clocks
class Service:
    """Read-only lookups against the orchestrators and the plane: where every timestamp comes from."""

    def __init__(self, args):
        import boto3
        from microvm import FleetManager, PlaneConfig

        self.args = args
        self.cfg = PlaneConfig()
        self.fm = FleetManager(self.cfg)
        self.session = boto3.Session(profile_name=self.cfg.profile, region_name=self.cfg.region)
        self._clients: dict = {}
        self._executions: dict = {}       # state machine ARN -> {execution name: execution ARN}

    def client(self, service):
        if service not in self._clients:
            self._clients[service] = self.session.client(service)
        return self._clients[service]

    def describe(self, kind: str, name: str, handle: dict, function: str | None = None) -> dict | None:
        """The orchestrator's own record once it is terminal, else None:
        {"status", "started_at", "stopped_at", "output"} from describe_execution or get_durable_execution."""
        if kind == "sfn":
            d = self.client("stepfunctions").describe_execution(executionArn=handle["execution_arn"])
            if d["status"] not in TERMINAL_SFN:
                return None
            return {"status": d["status"], "started_at": _epoch(d.get("startDate")),
                    "stopped_at": _epoch(d.get("stopDate")), "output": _json(d.get("output"))}
        lam = self.client("lambda")
        if not handle.get("execution_arn"):
            # the API refuses DurableExecutionName next to a Qualifier: look the name up on the bare function
            fn = (function or "").split(":")[0]
            items = lam.list_durable_executions_by_function(
                FunctionName=fn, DurableExecutionName=name).get("DurableExecutions", [])
            if not items:
                return None
            handle["execution_arn"] = items[0]["DurableExecutionArn"]
        d = lam.get_durable_execution(DurableExecutionArn=handle["execution_arn"])
        if d["Status"] not in TERMINAL_DURABLE:
            return None
        return {"status": d["Status"], "started_at": _epoch(d.get("StartTimestamp")),
                "stopped_at": _epoch(d.get("EndTimestamp")), "output": _json(d.get("Result"))}

    def sfn_execution_arn(self, state_machine_arn: str, name: str) -> str | None:
        """list_executions has no name filter: page through once per state machine and index by name."""
        index = self._executions.setdefault(state_machine_arn, {})
        if name in index:
            return index[name]
        pages = self.client("stepfunctions").get_paginator("list_executions").paginate(
            stateMachineArn=state_machine_arn)
        for page in pages:
            for e in page.get("executions", []):
                index[e["name"]] = e["executionArn"]
            if name in index:
                return index[name]
        return None

    def vm_times(self, vm_id: str) -> dict:
        """GetMicrovm's startedAt / terminatedAt as epochs and its state; {} if the plane forgot the VM."""
        try:
            d = self.fm.api.get_microvm(microvmIdentifier=vm_id)
        except Exception:
            return {}
        return {"started_at": _epoch(d.get("startedAt")), "terminated_at": _epoch(d.get("terminatedAt")),
                "state": d.get("state")}

    def fill_vm_times(self, rec) -> None:
        """startedAt / terminatedAt from the plane onto a Run or Member (never overwrite with nothing)."""
        t = self.vm_times(rec.vm_id) if rec.vm_id else {}
        rec.started_at = t.get("started_at") or rec.started_at
        rec.terminated_at = t.get("terminated_at") or rec.terminated_at


# ------------------------------------------------------------------------------- the bench
class Bench(Service):
    def __init__(self, args, console: Console):
        from microvm.lease import LeasePolicy

        super().__init__(args)
        self.console = console
        self.policy = LeasePolicy(budget_s=args.budget, heartbeat_timeout_s=90, slack_s=120)
        self.launched: set[str] = set()
        self.pool = futures.ThreadPoolExecutor(max_workers=8)

    # -- start -------------------------------------------------------------------------
    def start(self, run: Run) -> dict:
        """Kick off one lease; returns whatever `orchestrator_done` needs to find its end."""
        if run.kind == "sfn":
            resp = self.client("stepfunctions").start_execution(
                stateMachineArn=self.args.sfn_arn, name=run.name, input=json.dumps(TASK))
            return {"execution_arn": resp["executionArn"]}
        if run.kind == "durable":
            resp = self.client("lambda").invoke(
                FunctionName=self.args.durable_function, InvocationType="Event",
                Payload=json.dumps({"task": TASK}).encode(), DurableExecutionName=run.name)
            return {"execution_arn": resp.get("DurableExecutionArn")}
        from microvm.lease import Lease

        targets = {"sqs": self.args.queue_url, "eventbridge": self.args.bus, "http": self.args.http_target}
        target = targets[run.kind]
        lease = Lease(kind=run.kind, token=secrets.token_urlsafe(24), region=self.cfg.region, target=target,
                      id=run.name)
        vm = self.fm.lease(self.args.image, lease, TASK, self.policy)
        run.vm_id, run.t_seen = vm.microvm_id, time.time()
        self.launched.add(vm.microvm_id)
        poll_queue = self.args.queue_url if run.kind == "sqs" else self.args.collector_queue_url
        return {"token": lease.token, "queue_url": poll_queue}

    def start_fanout(self, fo: Fanout) -> dict:
        shards = [TASK] * fo.shards
        if fo.kind == "sfn":
            resp = self.client("stepfunctions").start_execution(
                stateMachineArn=self.args.sfn_map_arn, name=fo.name, input=json.dumps({"shards": shards}))
            return {"execution_arn": resp["executionArn"]}
        resp = self.client("lambda").invoke(
            FunctionName=self.args.durable_map_function, InvocationType="Event",
            Payload=json.dumps({"mode": "fanout", "shards": shards}).encode(), DurableExecutionName=fo.name)
        return {"execution_arn": resp.get("DurableExecutionArn")}

    # -- is the orchestrator done? ----------------------------------------------------------
    def orchestrator_done(self, run: Run, handle: dict) -> dict | None:
        """The orchestrator's terminal record (see Service.describe), or for the generic kinds the
        success/failure message as {"status": ...} with no service clock to offer."""
        if run.kind in ("sfn", "durable"):
            return self.describe(run.kind, run.name, handle, self.args.durable_function)
        status = self._poll_queue(run, handle)
        return {"status": status} if status else None

    def _poll_queue(self, run: Run, handle: dict) -> str | None:
        """What the generic controller does: read the VM's messages, keep the ones for this lease."""
        sqs = self.client("sqs")
        resp = sqs.receive_message(QueueUrl=handle["queue_url"], MaxNumberOfMessages=10, WaitTimeSeconds=1)
        outcome = None
        for msg in resp.get("Messages", []):
            try:
                body = json.loads(msg["Body"])
            except ValueError:
                continue
            if "detail" in body and "detail-type" in body:          # EventBridge envelope
                body = body["detail"] if isinstance(body["detail"], dict) else json.loads(body["detail"])
            mine = body.get("lease_id") == run.name or body.get("token") == handle["token"]
            if not mine:
                continue
            sqs.delete_message(QueueUrl=handle["queue_url"], ReceiptHandle=msg["ReceiptHandle"])
            status = body.get("status")
            if status in ("success", "failure"):
                outcome = "SUCCEEDED" if status == "success" else "FAILED"
                if isinstance(body.get("elapsed_s"), (int, float)):
                    run.vm_elapsed_s = float(body["elapsed_s"])
                if status == "failure":
                    run.vm_error = body.get("error")
        return outcome

    # -- watching the VM ---------------------------------------------------------------------
    def find_vm(self, run: Run, before: set[str]) -> None:
        for vm in self.fm.list(self.args.image):
            if vm.microvm_id not in before and vm.state != "TERMINATED":
                run.vm_id, run.t_seen = vm.microvm_id, time.time()
                run.started_at = vm.started_epoch or None
                self.launched.add(vm.microvm_id)
                return

    def vm_state(self, vm_id: str) -> str:
        return self.fm.get(vm_id).state

    def status_client(self, vm_id: str, endpoint: str | None = None):
        from microvm import EndpointClient

        if endpoint is None:
            endpoint = self.fm.get(vm_id).endpoint
        return EndpointClient(self.cfg, vm_id, endpoint=endpoint)

    def sample_status(self, run: Run, client) -> None:
        try:
            snap = client.status(timeout=STATUS_TIMEOUT_S, max_attempts=2)
        except Exception:                         # not serving yet, or auth token still minting
            return
        lease = snap.get("lease")
        if lease and run.t_lease is None and snap.get("started"):
            run.t_lease = iso_utc_to_epoch(snap["started"])
        if lease and lease.get("done") and run.t_vm_done is None:
            run.t_vm_done, run.work_s = time.time(), snap.get("elapsed_s")
            if lease.get("error"):
                run.vm_error = lease["error"]

    def wait_clear(self, image: str) -> None:
        """Never more than one run or fan-out at a time: wait for anything live on the image to clear."""
        end = time.time() + SETTLE_S
        while time.time() < end and any(v.state != "TERMINATED" for v in self.fm.list(image)):
            time.sleep(5)

    # -- one run -----------------------------------------------------------------------------
    def one(self, run: Run) -> Run:
        c = self.console
        before = {v.microvm_id for v in self.fm.list(self.args.image)}
        run.t0 = time.time()
        handle = self.start(run)
        c.print(f"  [cyan]{run.kind}[/] run {run.index + 1}: started {run.name}")
        client = None
        deadline = run.t0 + self.args.budget + 90
        while time.time() < deadline:
            if run.vm_id is None:
                self.find_vm(run, before)
            elif run.t_running is None:
                if self.vm_state(run.vm_id) == "RUNNING":
                    run.t_running = time.time()
                    c.print(f"    vm {run.vm_id} RUNNING at +{run.t_running - run.t0:.1f}s")
            else:
                if client is None:
                    client = self.status_client(run.vm_id)
                if run.t_vm_done is None:
                    self.sample_status(run, client)
                    if run.t_vm_done:
                        c.print(f"    lease accepted at +{(run.t_lease or 0) - run.t0:.1f}s, "
                                f"done after {run.work_s}s of work (+{run.t_vm_done - run.t0:.1f}s)")
            d = self.orchestrator_done(run, handle)
            if d:
                run.t_orch_done = time.time()
                apply_orchestrator(run, d)
                start, end, how = run.orchestrator_span()
                svc = f"{end - start:.1f}s by the {how}" if end else "no end time"
                c.print(f"    orchestrator {d['status']}: {svc}; "
                        f"seen by the bench at +{run.t_orch_done - run.t0:.1f}s")
                break
            time.sleep(POLL_S)
        else:
            run.notes.append("orchestrator did not finish within budget + 90 s")
        if run.kind in GENERIC and run.vm_id:
            try:
                self.fm.terminate(run.vm_id)      # the generic controller terminates; here that is us
            except Exception as e:
                run.notes.append(f"terminate: {e}")
        self.settle(run)
        return run

    def settle(self, run) -> None:
        """Wait for the VM to be TERMINATED (also the memory quota clearing for the next run), then ask
        GetMicrovm for the plane's own startedAt / terminatedAt."""
        if not run.vm_id:
            return
        end = time.time() + SETTLE_S
        while time.time() < end:
            state = self.vm_state(run.vm_id)
            if state == "TERMINATING" and getattr(run, "t_terminating", 0) is None:
                run.t_terminating = time.time()
            if state == "TERMINATED":
                run.t_terminated = time.time()
                self.launched.discard(run.vm_id)
                break
            time.sleep(2)
        else:
            run.notes.append(f"{run.vm_id} not TERMINATED after {SETTLE_S}s settle")
        if isinstance(run, Run):
            self.fill_vm_times(run)

    # -- one fan-out ---------------------------------------------------------------------------
    def scan_members(self, fo: Fanout, before: set[str] | None) -> None:
        """One ListMicrovms over the fan-out image: new members (never one alive `before` the start, so
        earlier fan-outs' leftovers stay out; None adds none at all, as in the settle), startedAt, and the
        first RUNNING / TERMINATING / TERMINATED sightings."""
        now = time.time()
        for vm in self.fm.list(fo.image):
            m = fo.members.get(vm.microvm_id)
            if m is None:
                if before is None or vm.microvm_id in before:
                    continue
                m = fo.members[vm.microvm_id] = Member(vm.microvm_id, now, endpoint=vm.endpoint)
                self.launched.add(vm.microvm_id)
            if m.endpoint is None and vm.endpoint:
                m.endpoint = vm.endpoint
            if m.started_at is None and vm.started_epoch:
                m.started_at = vm.started_epoch
            if vm.state == "RUNNING" and m.t_running is None:
                m.t_running = now
            if vm.state == "TERMINATING" and m.t_terminating is None:
                m.t_terminating = now
            if vm.state == "TERMINATED" and m.t_terminated is None:
                m.t_terminated = now
                self.launched.discard(vm.microvm_id)

    def sample_member(self, m: Member, clients: dict) -> None:
        try:
            if m.vm_id not in clients:
                clients[m.vm_id] = self.status_client(m.vm_id, m.endpoint)
            snap = clients[m.vm_id].status(timeout=STATUS_TIMEOUT_S, max_attempts=2)
        except Exception:                         # not serving yet, or already gone
            return
        lease = snap.get("lease")
        if lease and lease.get("done") and m.t_done is None:
            m.t_done, m.work_s = time.time(), snap.get("elapsed_s")
            if lease.get("error"):
                m.error = lease["error"]

    def one_fanout(self, fo: Fanout) -> Fanout:
        c = self.console
        before = {v.microvm_id for v in self.fm.list(fo.image)}
        fo.t0 = time.time()
        handle = self.start_fanout(fo)
        c.print(f"  [cyan]{fo.kind}[/] fan-out of {fo.shards} on {fo.image}: started {fo.name}")
        clients: dict = {}
        first_done_printed = False
        deadline = fo.t0 + self.args.budget + 180
        while time.time() < deadline:
            self.scan_members(fo, before)
            if fo.t_all_running is None and fo.running_count() >= fo.shards:
                fo.t_all_running = time.time()
                c.print(f"    all {fo.shards} RUNNING at +{fo.t_all_running - fo.t0:.1f}s")
            pending = [m for m in fo.members.values()
                       if m.t_running and m.t_done is None and not (m.t_terminating or m.t_terminated)]
            if pending:
                list(self.pool.map(lambda m: self.sample_member(m, clients), pending))
            done = [m for m in fo.members.values() if m.t_done]
            if done and not first_done_printed:
                first_done_printed = True
                c.print(f"    first shard done at +{min(m.t_done for m in done) - fo.t0:.1f}s")
            d = self.describe(fo.kind, fo.name, handle, self.args.durable_map_function)
            if d:
                fo.t_orch_done = time.time()
                apply_orchestrator(fo, d)
                start, end, how = fo.orchestrator_span()
                svc = f"{end - start:.1f}s by the {how}" if end else "no end time"
                c.print(f"    orchestrator {d['status']}: {svc}; "
                        f"seen by the bench at +{fo.t_orch_done - fo.t0:.1f}s; output has "
                        f"{fo.output_shards}/{fo.shards} shards, {len(done)} reported done over /status")
                break
            time.sleep(POLL_S)
        else:
            fo.notes.append("orchestrator did not finish within budget + 180 s")
        if len(fo.members) != fo.shards:
            fo.notes.append(f"saw {len(fo.members)} members for {fo.shards} shards")
        self.settle_fanout(fo, before)
        return fo

    def settle_fanout(self, fo: Fanout, before: set[str] | None = None) -> None:
        """Wait until every member is TERMINATED, then take startedAt / terminatedAt from GetMicrovm."""
        end = time.time() + SETTLE_S
        live: list = []
        while time.time() < end:
            self.scan_members(fo, before)
            live = [m for m in fo.members.values() if not m.t_terminated]
            if not live:
                break
            time.sleep(2)
        else:
            fo.notes.append(f"{len(live)} member(s) not TERMINATED after {SETTLE_S}s settle")
        list(self.pool.map(self.fill_vm_times, fo.members.values()))

    # -- in-VM parallel steps --------------------------------------------------------------------
    def one_parallel(self, pr: ParallelRun) -> ParallelRun:
        from microvm.lease import Lease

        c = self.console
        task = {"steps": list(PARALLEL_STEPS)}
        if pr.mode == "parallel":
            task["parallel"] = True
        pr.t0 = time.time()
        vm = self.fm.lease(self.args.image, Lease(kind="none", id=pr.name), task, self.policy)
        pr.vm_id = vm.microvm_id
        self.launched.add(pr.vm_id)
        c.print(f"  [cyan]in-VM {pr.mode}[/]: {pr.vm_id} with {len(PARALLEL_STEPS)} x `sleep 3`")
        client = None
        deadline = pr.t0 + self.args.budget + 90
        while time.time() < deadline:
            if pr.t_running is None:
                if self.vm_state(pr.vm_id) == "RUNNING":
                    pr.t_running = time.time()
                    c.print(f"    RUNNING at +{pr.t_running - pr.t0:.1f}s")
            else:
                if client is None:
                    client = self.status_client(pr.vm_id, vm.endpoint)
                try:
                    snap = client.status(timeout=STATUS_TIMEOUT_S, max_attempts=2)
                except Exception:
                    snap = None
                lease = (snap or {}).get("lease")
                if lease and lease.get("done"):
                    pr.t_done, pr.elapsed_s = time.time(), snap.get("elapsed_s")
                    pr.error = lease.get("error") or None
                    pr.passed = pr.error is None
                    result = _result_from_snapshot(snap)
                    if result and isinstance(result.get("steps"), list):
                        pr.step_seconds = round(
                            sum(float(s.get("duration_s") or 0) for s in result["steps"]), 3)
                    c.print(f"    done: elapsed {pr.elapsed_s}s on the VM"
                            + (f", steps summed {pr.step_seconds}s" if pr.step_seconds is not None else "")
                            + (f", error {pr.error.get('error_type')}" if pr.error else ""))
                    break
            time.sleep(POLL_S)
        else:
            pr.notes.append("lease not done within budget + 90 s")
        try:
            self.fm.terminate(pr.vm_id)           # kind none: nobody else terminates
        except Exception as e:
            pr.notes.append(f"terminate: {e}")
        self.settle(pr)
        return pr

    # -- refusal drill --------------------------------------------------------------------------
    def refusal_drill(self) -> dict:
        """fm.plan(40, 2048, LeasePolicy()) printed, then the launcher asked for all 40 at once with
        RunMicrovm trip-wired: the refusal must come before any launch. Never launches anything."""
        from microvm.lease import Lease, LeasePolicy

        out: dict = {"shards": DRILL_SHARDS, "baseline_mib": DRILL_BASELINE_MIB, "launched": 0,
                     "run_microvm_calls": 0, "refusal": None, "refused_by": None, "ok": False}
        try:
            from microvm.lease import LeasePlanRejected
        except ImportError:
            out["error"] = "microvm-ctl >= 0.3.0 is required (FleetManager.plan, LeasePlanRejected)"
            self.console.print(f"  [red]{out['error']}[/]")
            return out
        live_before = {v.microvm_id for v in self.fm.list() if v.state != "TERMINATED"}
        plan = self.fm.plan(DRILL_SHARDS, DRILL_BASELINE_MIB, LeasePolicy())
        out.update({"summary": plan.summary(), "plan_rejected": plan.rejected,
                    "concurrency": plan.concurrency, "waves": plan.waves,
                    "memory_quota_gb": plan.limit.memory_quota_gb,
                    "worst_case_vm_seconds": plan.worst_case_vm_seconds,
                    "worst_case_usd": plan.worst_case_usd})
        self.console.print(f"  plan: {plan.summary()}")
        if plan.rejected:
            out["refusal"], out["refused_by"] = plan.rejected, "plan"
        else:
            calls: list = []
            real_run = self.fm._run

            def tripwire(**kw):
                calls.append(kw)
                raise RuntimeError("tripwire: RunMicrovm was called during the refusal drill")

            self.fm._run = tripwire
            try:
                leases = [Lease(kind="none", id=f"drill-{i}") for i in range(DRILL_SHARDS)]
                self.fm.lease_many(self.args.image, leases, [TASK] * DRILL_SHARDS, LeasePolicy(),
                                   baseline_mib=DRILL_BASELINE_MIB)
                out["note"] = "lease_many did not refuse"
            except LeasePlanRejected as e:
                out["refusal"], out["refused_by"] = str(e), "lease_many"
            except RuntimeError as e:
                out["note"] = str(e)
            finally:
                self.fm._run = real_run
            out["run_microvm_calls"] = len(calls)
        live_after = {v.microvm_id for v in self.fm.list() if v.state != "TERMINATED"}
        out["launched"] = len(live_after - live_before)
        out["ok"] = bool(out["refusal"]) and out["launched"] == 0 and out["run_microvm_calls"] == 0
        colour = "green" if out["ok"] else "red"
        self.console.print(f"  [{colour}]refused by {out['refused_by']}: {out['refusal']}[/] "
                           f"(RunMicrovm calls {out['run_microvm_calls']}, new VMs {out['launched']})")
        return out

    def cleanup(self) -> None:
        for vm_id in list(self.launched):
            try:
                if self.fm.get(vm_id).state != "TERMINATED":
                    self.fm.terminate(vm_id)
                    self.console.print(f"[dim]terminated leftover {vm_id}[/]")
            except Exception as e:
                self.console.print(f"[red]could not terminate {vm_id}: {e}[/]")
        self.pool.shutdown(wait=False)


# ------------------------------------------------------------------------------- reporting
def aggregate(rows: list[dict]) -> dict:
    """p50/min/max/n per metric over a list of metric dicts (None values skipped)."""
    agg: dict = {}
    for key in rows[0] if rows else []:
        xs = [r[key] for r in rows if isinstance(r.get(key), (int, float)) and not isinstance(r[key], bool)]
        nd = 6 if key == "cost_usd" else 3
        agg[key] = ({"p50": round(statistics.median(xs), nd), "min": round(min(xs), nd),
                     "max": round(max(xs), nd), "n": len(xs)} if xs else None)
    return agg


def summarise(runs: list[Run], max_duration: int) -> dict:
    out: dict = {}
    for kind in KINDS:
        mine = [r for r in runs if r.kind == kind]
        if not mine:
            continue
        out[kind] = {"runs": len(mine), "ok": sum(1 for r in mine if r.orchestrator_status == "SUCCEEDED"),
                     **aggregate([r.metrics(max_duration) for r in mine])}
    return out


def summarise_fanouts(rows: list[dict]) -> dict:
    """rows: [{"kind", "shards", "ok", "metrics": {...}}] -> {"sfn x4": {...}, ...}"""
    out: dict = {}
    for kind in FANOUT_KINDS:
        for n in sorted({r["shards"] for r in rows if r["kind"] == kind}):
            mine = [r for r in rows if r["kind"] == kind and r["shards"] == n]
            out[f"{kind} x{n}"] = {"kind": kind, "shards": n, "runs": len(mine),
                                    "ok": sum(1 for r in mine if r["ok"]),
                                    **aggregate([r["metrics"] for r in mine])}
    return out


def summarise_parallel(rows: list[dict]) -> dict:
    out: dict = {}
    for mode in ("sequential", "parallel"):
        mine = [r for r in rows if r["mode"] == mode]
        if mine:
            out[mode] = {"runs": len(mine), "ok": sum(1 for r in mine if r.get("passed")),
                         **aggregate([r["metrics"] for r in mine])}
    return out


def _cell(agg, key, fmt="{:.1f}s"):
    v = (agg.get(key) or {}).get("p50")
    return fmt.format(v) if v is not None else "-"


def table(summary: dict) -> Table:
    t = Table(title="lease handoff, p50 per kind", header_style="bold magenta")
    for col in ("kind", "ok", "launch to lease", "work", "completion to resume", "end to end", "VM s",
                "cost / lease"):
        t.add_column(col, justify="right" if col != "kind" else "left")
    for kind, agg in summary.items():
        t.add_row(kind, f"{agg['ok']}/{agg['runs']}", _cell(agg, "launch_to_lease_s"), _cell(agg, "work_s"),
                  _cell(agg, "completion_to_resume_s"), _cell(agg, "end_to_end_s"),
                  _cell(agg, "vm_seconds", "{:.0f}"), _cell(agg, "cost_usd", "${:.5f}"))
    return t


def fanout_table(summary: dict) -> Table:
    t = Table(title="fan-out, p50 per kind and size", header_style="bold magenta")
    for col in ("kind", "shards", "ok", "all running", "first shard done", "slowest shard done", "end to end",
                "VM s total", "cost / fan-out"):
        t.add_column(col, justify="right" if col != "kind" else "left")
    for agg in summary.values():
        t.add_row(agg["kind"], str(agg["shards"]), f"{agg['ok']}/{agg['runs']}",
                  _cell(agg, "launch_to_all_running_s"), _cell(agg, "first_shard_done_s"),
                  _cell(agg, "slowest_shard_done_s"), _cell(agg, "end_to_end_s"),
                  _cell(agg, "vm_seconds_total", "{:.0f}"), _cell(agg, "cost_usd", "${:.5f}"))
    return t


def parallel_table(summary: dict) -> Table:
    t = Table(title="in-VM parallel: four `sleep 3` steps on one 2 GB VM", header_style="bold magenta")
    for col in ("mode", "ok", "launch to running", "VM elapsed", "steps summed", "VM s", "cost"):
        t.add_column(col, justify="right" if col != "mode" else "left")
    for mode, agg in summary.items():
        t.add_row(mode, f"{agg['ok']}/{agg['runs']}", _cell(agg, "launch_to_running_s"),
                  _cell(agg, "elapsed_s"), _cell(agg, "step_seconds"), _cell(agg, "vm_seconds", "{:.0f}"),
                  _cell(agg, "cost_usd", "${:.5f}"))
    return t


def drill_table(drills: list[dict]) -> Table:
    from rich.markup import escape

    t = Table(title="refusal drill: plan 40 x 2 GB, launch refused before RunMicrovm",
              header_style="bold magenta")
    for col in ("shards", "baseline", "quota", "refused by", "sentence", "RunMicrovm calls", "new VMs", "ok"):
        t.add_column(col, justify="left" if col in ("refused by", "sentence") else "right")
    for d in drills:
        quota = d.get("memory_quota_gb")
        sentence = d.get("refusal") or d.get("error") or d.get("note") or "-"
        t.add_row(str(d.get("shards")), f"{d.get('baseline_mib')} MiB", f"{quota:g} GB" if quota else "-",
                  str(d.get("refused_by") or "-"), escape(str(sentence)),
                  str(d.get("run_microvm_calls", "-")), str(d.get("launched", "-")),
                  "[green]yes[/]" if d.get("ok") else "[red]no[/]")
    return t


def print_tables(console: Console, summary, fanout_summary, parallel_summary, drills) -> None:
    if summary:
        console.print(table(summary))
    if fanout_summary:
        console.print(fanout_table(fanout_summary))
    if parallel_summary:
        console.print(parallel_table(parallel_summary))
    if drills:
        console.print(drill_table(drills))


def summarize_files(paths: list[str]) -> None:
    """Merge the result files of one session into the tables, one JSON, and one SVG."""
    merged: dict = {}
    fanouts: list[dict] = []
    parallel: list[dict] = []
    drills: list[dict] = []
    files = [Path(p) for p in paths] or sorted(Path("benchmarks/results").glob("handoff-*.json"))
    for path in files:
        data = json.loads(path.read_text())
        for run in data.get("runs", []):
            metrics = dict(run["metrics"])
            if metrics.get("vm_seconds") is not None:   # recompute: early files rounded the cost away
                metrics["cost_usd"] = round(
                    metrics["vm_seconds"] * VM_USD_PER_S + ORCHESTRATOR_USD[run["kind"]], 6)
            merged.setdefault(run["kind"], []).append({**run, "metrics": metrics})
        fanouts.extend(data.get("fanouts") or [])
        parallel.extend(data.get("in_vm_parallel") or [])
        if data.get("refusal_drill"):
            drills.append(data["refusal_drill"])
    summary = {}
    for kind in KINDS:
        rows = merged.get(kind)
        if rows:
            ok = sum(r["orchestrator_status"] == "SUCCEEDED" for r in rows)
            summary[kind] = {"runs": len(rows), "ok": ok, **aggregate([r["metrics"] for r in rows])}
    fanout_summary = summarise_fanouts(fanouts)
    parallel_summary = summarise_parallel(parallel)
    console = Console(record=True, width=120)
    console.rule("[bold]lease handoff bench: all kinds, one image, one account")
    print_tables(console, summary, fanout_summary, parallel_summary, drills)
    console.print(f"cost model: VM seconds x ${VM_USD_PER_S:.10f} (2 GB tier) + orchestrator ops "
                  f"{ORCHESTRATOR_USD}; fan-outs: VM seconds x baseline GB x ${VM_USD_PER_GB_S:.10f} "
                  f"+ ops x shards; files: {', '.join(p.name for p in files)}")
    out_dir = Path("benchmarks/results/handoff")
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"summary": summary, "fanout": fanout_summary, "in_vm_parallel": parallel_summary,
               "refusal_drill": drills, "sources": [p.name for p in files],
               "rates": {"vm_usd_per_s": VM_USD_PER_S, "vm_usd_per_gb_s": VM_USD_PER_GB_S,
                         "orchestrator_usd": ORCHESTRATOR_USD}}
    (out_dir / "summary.json").write_text(json.dumps(payload, indent=2))
    console.save_svg(str(out_dir / "summary.svg"), title="mvm lease handoff bench")
    console.print(f"summary -> {out_dir / 'summary.json'} (+ .svg)")


# ------------------------------------------------------------------------------- backfill
def _from_record(cls, rec: dict):
    """Rebuild a dataclass from a result record: fields other versions did not write take their defaults,
    keys that are not fields (metrics, ok) are left out."""
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in rec.items() if k in names})


def _delta(before: dict, after: dict, key: str, fmt="{:.3f}") -> str:
    def show(v):
        return fmt.format(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)
    return f"{key} {show(before.get(key))} -> {show(after.get(key))}"


def backfill_run(svc: Service, args, run: Run) -> None:
    """Service start/stop and output for one recorded run, plus the plane's times for its VM."""
    d = None
    if run.kind == "sfn":
        if not args.sfn_arn:
            _note(run, "backfill: --sfn-arn missing, orchestrator times kept as recorded")
        else:
            arn = svc.sfn_execution_arn(args.sfn_arn, run.name)
            if arn:
                d = svc.describe("sfn", run.name, {"execution_arn": arn})
            else:
                _note(run, f"backfill: execution {run.name} not found on --sfn-arn")
    elif run.kind == "durable":
        if not args.durable_function:
            _note(run, "backfill: --durable-function missing, orchestrator times kept as recorded")
        else:
            d = svc.describe("durable", run.name, {}, args.durable_function)
            if d is None:
                _note(run, f"backfill: durable execution {run.name} not found or not terminal")
    if d:
        apply_orchestrator(run, d)
    svc.fill_vm_times(run)


def backfill_fanout(svc: Service, args, fo: Fanout) -> None:
    """Service start/stop and output for one recorded fan-out; startedAt / terminatedAt per member; and the
    members an over-eager settle scan swept in from earlier fan-outs are dropped by their startedAt."""
    d = None
    if fo.kind == "sfn":
        if not args.sfn_map_arn:
            _note(fo, "backfill: --sfn-map-arn missing, orchestrator times kept as recorded")
        else:
            arn = svc.sfn_execution_arn(args.sfn_map_arn, fo.name)
            if arn:
                d = svc.describe("sfn", fo.name, {"execution_arn": arn})
            else:
                _note(fo, f"backfill: execution {fo.name} not found on --sfn-map-arn")
    else:
        if not args.durable_map_function:
            _note(fo, "backfill: --durable-map-function missing, orchestrator times kept as recorded")
        else:
            d = svc.describe("durable", fo.name, {}, args.durable_map_function)
            if d is None:
                _note(fo, f"backfill: durable execution {fo.name} not found or not terminal")
    if d:
        apply_orchestrator(fo, d)
    for m in fo.members.values():
        svc.fill_vm_times(m)
    start, end, _ = fo.orchestrator_span()
    if start and end:
        stale = [vid for vid, m in fo.members.items()
                 if (m.started_at and not (start - 1.0 <= m.started_at <= end + 1.0))
                 or (m.started_at is None and m.t_running is None)]
        for vid in stale:
            del fo.members[vid]
        if stale:
            _note(fo, f"backfill: dropped {len(stale)} member(s) the settle scan swept in from earlier runs")
    fo.notes = [n for n in fo.notes if not n.startswith("saw ")]
    if len(fo.members) != fo.shards:
        _note(fo, f"saw {len(fo.members)} members for {fo.shards} shards")


def backfill_files(paths: list[str], args, console: Console) -> None:
    """Rewrite result files in place with the services' own timestamps. Read-only APIs; launches nothing."""
    from microvm.lease import LeasePolicy

    svc = Service(args)
    console.rule("[bold]handoff bench: backfill from the services' clocks")
    for p in paths:
        path = Path(p)
        data = json.loads(path.read_text())
        max_duration = LeasePolicy(budget_s=data.get("budget_s") or 300, heartbeat_timeout_s=90,
                                   slack_s=120).max_duration()
        console.print(f"[bold]{path.name}[/]")
        runs: list[Run] = []
        for rec in data.get("runs", []):
            run = _from_record(Run, rec)
            before = rec.get("metrics") or {}
            backfill_run(svc, args, run)
            after = run.metrics(max_duration)
            console.print(f"  {run.name}: " + ", ".join(
                _delta(before, after, k) for k in ("end_to_end_s", "completion_to_resume_s", "vm_seconds")))
            runs.append(run)
        fanout_rows: list[dict] = []
        for rec in data.get("fanouts") or []:
            fo = _from_record(Fanout, rec)
            fo.members = {vid: _from_record(Member, m) for vid, m in (fo.members or {}).items()}
            before = {**(rec.get("metrics") or {}), "ok": rec.get("ok")}
            backfill_fanout(svc, args, fo)
            metrics = fo.metrics(max_duration)
            after = {**metrics, "ok": fo.ok()}       # the print only: `ok` is not a metric
            console.print(f"  {fo.name}: " + ", ".join(
                _delta(before, after, k, "{}") if k in ("ok", "members_seen") else _delta(before, after, k)
                for k in ("ok", "end_to_end_s", "vm_seconds_total", "members_seen")))
            fanout_rows.append({**asdict(fo), "ok": fo.ok(), "metrics": metrics})
        data["runs"] = [{**asdict(r), "metrics": r.metrics(max_duration)} for r in runs]
        data["summary"] = summarise(runs, max_duration)
        if "fanouts" in data or fanout_rows:
            data["fanouts"] = fanout_rows
            data["fanout_summary"] = summarise_fanouts(fanout_rows)
        data["backfilled"] = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "from": "describe_execution startDate/stopDate, get_durable_execution Start/EndTimestamp, "
                    "GetMicrovm startedAt/terminatedAt"}
        path.write_text(json.dumps(data, indent=2, default=str))
    console.print(f"[dim]rewrote {len(paths)} file(s); the .svg transcripts are untouched. "
                  "Nothing was launched.[/]")


def plan(args, kinds: list[str], fanout_sizes: list[int], fanout_kinds: list[str], console: Console) -> None:
    console.rule("[bold]handoff bench: dry run")
    console.print(f"image [cyan]{args.image}[/], task {json.dumps(TASK)}, {args.runs} run(s) per kind, "
                  f"budget {args.budget}s, one VM at a time, {SETTLE_S}s settle for TERMINATED between runs")
    how = {
        "sfn": "start_execution on --sfn-arn; done when describe-execution leaves RUNNING; end_to_end_s = "
               "stopDate - startDate",
        "durable": "lambda.invoke (Event, DurableExecutionName) on --durable-function; done when "
                   "get-durable-execution is terminal; end_to_end_s = EndTimestamp - StartTimestamp",
        "sqs": "FleetManager.lease(kind=sqs, target=--queue-url); done on the success/failure message "
               "(arrival includes the poll wait)",
        "eventbridge": "FleetManager.lease(kind=eventbridge, target=--bus); done when the rule delivers the "
                       "success/failure event to --collector-queue-url (arrival includes the poll wait)",
        "http": "FleetManager.lease(kind=http, target=--http-target); done when the collector appends the "
                "success/failure POST to --collector-queue-url (arrival includes the poll wait)",
    }
    for kind in kinds:
        missing = [f"--{a.replace('_', '-')}" for a in REQUIRED[kind] if not getattr(args, a)]
        flag = f"[red]missing {', '.join(missing)}[/]" if missing else "[green]args ok[/]"
        console.print(f"  [cyan]{kind:<11}[/] {how[kind]}  {flag}")
    if kinds:
        console.print("per run: fm.list every 0.5 s for the new VM and RUNNING; GET /status for `started` "
                      "(lease accepted) and lease.done; the orchestrator's own start/stop once terminal "
                      "(completion_to_resume_s = stop - (started + the payload's elapsed_s), clamped at 0); "
                      "then wait for TERMINATED; VM-seconds from GetMicrovm startedAt -> terminatedAt.")
    if fanout_sizes:
        gb = args.baseline_mib / 1024
        console.print(f"fan-outs on [cyan]{args.fanout_image}[/] ({args.baseline_mib} MiB baseline), "
                      f"one at a time, {SETTLE_S}s settle until every member is TERMINATED:")
        how_map = {    # the backslash keeps Rich from reading [task x N] as a markup tag
            "sfn": "start_execution on --sfn-map-arn with {\"shards\": \\[task x N]} (the Map machine, "
                   "template-map.yaml deployed with ImageName=--fanout-image)",
            "durable": "lambda.invoke (Event) on --durable-map-function with {\"mode\": \"fanout\", "
                       "\"shards\": \\[task x N]} (durable-handoff deployed with ImageName=--fanout-image; "
                       "calls lease_map)",
        }
        for kind in fanout_kinds:
            missing = [f"--{a.replace('_', '-')}" for a in REQUIRED_FANOUT[kind] if not getattr(args, a)]
            flag = f"[red]missing {', '.join(missing)}[/]" if missing else "[green]args ok[/]"
            for n in fanout_sizes:
                console.print(f"  [cyan]{kind:<8} x{n:<3}[/] {how_map[kind]}  {flag}")
        console.print("per fan-out: fm.list(image) every 0.5 s until all N members have been RUNNING "
                      "(launch_to_all_running_s); GET /status on every RUNNING member (thread pool of 8) for "
                      "lease.done (first_shard_done_s, slowest_shard_done_s, informational); the "
                      "orchestrator's own start/stop (end_to_end_s) and output (ok = SUCCEEDED with N shards "
                      "in the output: sfn N payloads, durable succeeded == N); VM-seconds summed over "
                      "members from GetMicrovm "
                      "startedAt to terminatedAt (else the first TERMINATING/TERMINATED seen), capped at "
                      f"budget + slack (vm_seconds_total); cost = VM-s x {gb:g} GB x ${VM_USD_PER_GB_S:.10f} "
                      "+ orchestrator ops x N.")
    if args.in_vm_parallel:
        console.print(f"in-VM parallel on [cyan]{args.image}[/] (2 GB): two leases of kind none, "
                      f"{json.dumps({'steps': PARALLEL_STEPS})} sequential, then the same with "
                      "\"parallel\": true (needs the 0.3.0 handoff-agent); elapsed_s and the summed step "
                      "durations from the VM's /status; terminated by the bench; one VM at a time.")
    if args.refusal_drill:
        console.print(f"refusal drill: fm.plan({DRILL_SHARDS}, {DRILL_BASELINE_MIB}, LeasePolicy()) printed; "
                      "if the plan allows waves, fm.lease_many with all 40 at once must refuse "
                      "(LeasePlanRejected) with RunMicrovm trip-wired; asserts a refusal sentence, zero "
                      "RunMicrovm calls, zero new VMs.")
    console.print(f"cost model: VM seconds x ${VM_USD_PER_S:.10f} (2 GB tier) + orchestrator ops "
                  f"{ {k: round(v, 6) for k, v in ORCHESTRATOR_USD.items()} }")
    console.print(f"output: {RESULTS}/handoff-<timestamp>.json and .svg. Nothing was launched.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--kinds", default=None,
                    help=f"comma list from {','.join(KINDS)}; default all of them unless only --fanout, "
                         "--in-vm-parallel, or --refusal-drill is asked for")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--image", default="handoff-agent")
    ap.add_argument("--budget", type=int, default=300,
                    help="lease budget for the generic kinds and the poll cap")
    ap.add_argument("--sfn-arn", help="state machine ARN (stepfunctions-handoff stack output)")
    ap.add_argument("--durable-function",
                    help="durable orchestrator name[:alias], deployed with ImageName=handoff-agent")
    ap.add_argument("--queue-url", help="SQS queue the VM writes to for --kinds sqs")
    ap.add_argument("--bus", help="EventBridge bus name for --kinds eventbridge")
    ap.add_argument("--http-target", help="collector Function URL for --kinds http")
    ap.add_argument("--collector-queue-url", help="queue the eventbridge rule / http collector deliver into")
    fan = ap.add_argument_group("leases at scale")
    fan.add_argument("--fanout", help="comma list of shard counts, e.g. 4,8; one fan-out at a time")
    fan.add_argument("--fanout-kinds", default=",".join(FANOUT_KINDS), help=f"from {','.join(FANOUT_KINDS)}")
    fan.add_argument("--fanout-image", default="handoff-agent-small",
                     help="the image the fan-outs lease (a smaller build of the same agent)")
    fan.add_argument("--baseline-mib", type=int, default=512, help="memory of --fanout-image, for the cost")
    fan.add_argument("--sfn-map-arn",
                     help="the Map state machine ARN (stepfunctions-handoff template-map.yaml)")
    fan.add_argument("--durable-map-function",
                     help="durable orchestrator name[:alias] deployed with ImageName=--fanout-image "
                          "(default: --durable-function)")
    fan.add_argument("--in-vm-parallel", action="store_true",
                     help="one 2 GB VM, four `sleep 3` steps sequential then parallel")
    fan.add_argument("--refusal-drill", action="store_true",
                     help="fm.plan(40, 2048) and the pre-flight refusal, nothing launched")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit; touches nothing")
    ap.add_argument("--summarize", nargs="*", metavar="RESULT_JSON",
                    help="merge earlier result files into one table, JSON, and SVG; launches nothing")
    ap.add_argument("--backfill", nargs="+", metavar="RESULT_JSON",
                    help="rewrite earlier result files in place with the services' own timestamps for their "
                         "recorded executions (needs the matching --sfn-arn/--sfn-map-arn/--durable-function/"
                         "--durable-map-function); read-only APIs, launches nothing")
    args = ap.parse_args(argv)
    if args.summarize is not None:
        summarize_files(args.summarize)
        return 0
    args.durable_map_function = args.durable_map_function or args.durable_function
    if args.backfill:
        backfill_files(args.backfill, args, Console(width=120))
        return 0

    extensions = bool(args.fanout or args.in_vm_parallel or args.refusal_drill)
    if args.kinds is None:
        kinds = [] if extensions else list(KINDS)
    else:
        kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        ap.error(f"unknown kinds {bad}; choose from {', '.join(KINDS)}")
    fanout_sizes: list[int] = []
    fanout_kinds: list[str] = []
    if args.fanout:
        try:
            fanout_sizes = [int(x) for x in args.fanout.split(",") if x.strip()]
        except ValueError:
            ap.error("--fanout wants a comma list of integers, e.g. 4,8")
        if any(n < 1 for n in fanout_sizes):
            ap.error("--fanout sizes must be positive")
        fanout_kinds = [k.strip() for k in args.fanout_kinds.split(",") if k.strip()]
        bad = [k for k in fanout_kinds if k not in FANOUT_KINDS]
        if bad:
            ap.error(f"unknown fan-out kinds {bad}; choose from {', '.join(FANOUT_KINDS)}")
    console = Console(record=True, width=120)
    if args.dry_run:
        plan(args, kinds, fanout_sizes, fanout_kinds, console)
        return 0
    for kind in kinds:
        missing = [f"--{a.replace('_', '-')}" for a in REQUIRED[kind] if not getattr(args, a)]
        if missing:
            ap.error(f"kind {kind} needs {', '.join(missing)}")
    for kind in fanout_kinds:
        missing = [f"--{a.replace('_', '-')}" for a in REQUIRED_FANOUT[kind] if not getattr(args, a)]
        if missing:
            ap.error(f"fan-out kind {kind} needs {', '.join(missing)}")

    bench = Bench(args, console)
    what = [f"{', '.join(kinds)} x {args.runs} on {args.image}"] if kinds else []
    if fanout_sizes:
        what.append(f"fan-outs {fanout_sizes} ({', '.join(fanout_kinds)}) on {args.fanout_image}")
    if args.in_vm_parallel:
        what.append("in-VM parallel")
    if args.refusal_drill:
        what.append("refusal drill")
    console.rule(f"[bold]lease handoff bench: {'; '.join(what)}")
    runs: list[Run] = []
    fanouts: list[Fanout] = []
    parallel: list[ParallelRun] = []
    drill: dict | None = None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    max_duration = bench.policy.max_duration()
    try:
        for kind in kinds:
            for i in range(args.runs):
                bench.wait_clear(args.image)      # never more than one VM
                run = Run(kind=kind, index=i, name=f"bench-{kind}-{stamp}-{i}")
                try:
                    runs.append(bench.one(run))
                except Exception as e:
                    run.notes.append(f"failed: {type(e).__name__}: {e}")
                    console.print(f"    [red]{run.name} failed: {e}[/]")
                    runs.append(run)
                    bench.settle(run)
        for n in fanout_sizes:
            for kind in fanout_kinds:
                bench.wait_clear(args.fanout_image)      # one fan-out at a time
                fo = Fanout(kind=kind, shards=n, name=f"bench-{kind}-map{n}-{stamp}", image=args.fanout_image,
                            baseline_mib=args.baseline_mib)
                try:
                    fanouts.append(bench.one_fanout(fo))
                except Exception as e:
                    fo.notes.append(f"failed: {type(e).__name__}: {e}")
                    console.print(f"    [red]{fo.name} failed: {e}[/]")
                    fanouts.append(fo)
                    bench.settle_fanout(fo)
        if args.in_vm_parallel:
            for mode in ("sequential", "parallel"):
                bench.wait_clear(args.image)
                pr = ParallelRun(mode=mode, name=f"bench-invm-{mode}-{stamp}")
                try:
                    parallel.append(bench.one_parallel(pr))
                except Exception as e:
                    pr.notes.append(f"failed: {type(e).__name__}: {e}")
                    console.print(f"    [red]{pr.name} failed: {e}[/]")
                    parallel.append(pr)
                    bench.settle(pr)
        if args.refusal_drill:
            console.print("  [cyan]refusal drill[/]")
            try:
                drill = bench.refusal_drill()
            except Exception as e:
                drill = {"shards": DRILL_SHARDS, "baseline_mib": DRILL_BASELINE_MIB, "ok": False,
                         "error": f"{type(e).__name__}: {e}"}
                console.print(f"    [red]drill failed: {e}[/]")
    finally:
        bench.cleanup()
        summary = summarise(runs, max_duration)
        fanout_rows = [{**asdict(f), "ok": f.ok(), "metrics": f.metrics(max_duration)} for f in fanouts]
        parallel_rows = [{**asdict(p), "metrics": p.metrics(max_duration)} for p in parallel]
        fanout_summary = summarise_fanouts(fanout_rows)
        parallel_summary = summarise_parallel(parallel_rows)
        print_tables(console, summary, fanout_summary, parallel_summary, [drill] if drill else [])
        RESULTS.mkdir(exist_ok=True)
        out = {"date": time.strftime("%Y-%m-%d"), "region": bench.cfg.region, "image": args.image,
               "task": TASK, "budget_s": args.budget,
               "fanout_image": args.fanout_image if fanouts else None,
               "baseline_mib": args.baseline_mib if fanouts else None,
               "rates": {"vm_usd_per_s": VM_USD_PER_S, "vm_usd_per_gb_s": VM_USD_PER_GB_S,
                         "orchestrator_usd": ORCHESTRATOR_USD},
               "summary": summary, "fanout_summary": fanout_summary, "parallel_summary": parallel_summary,
               "runs": [{**asdict(r), "metrics": r.metrics(max_duration)} for r in runs],
               "fanouts": fanout_rows, "in_vm_parallel": parallel_rows, "refusal_drill": drill}
        (RESULTS / f"handoff-{stamp}.json").write_text(json.dumps(out, indent=2, default=str))
        console.save_svg(str(RESULTS / f"handoff-{stamp}.svg"), title="awesome-microvm - lease handoff bench")
        console.print(f"[dim]results -> benchmarks/results/handoff-{stamp}.json (+ .svg)[/]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
