"""Benchmark the lease handoff: how long from "start" to the VM working, from the VM's
completion to the orchestrator resuming, end to end, and what one lease costs.

    MVM_PROFILE=me python3 benchmarks/handoff_bench.py --runs 3 --kinds sfn,durable,sqs,eventbridge,http \
        --sfn-arn arn:aws:states:...:stateMachine:microvm-sfn-handoff-lease \
        --durable-function microvm-durable-handoff-orchestrator:live \
        --queue-url https://sqs.../microvm-lease-sqs --bus microvm-lease \
        --http-target https://<collector>.lambda-url.us-east-1.on.aws/ \
        --collector-queue-url https://sqs.../microvm-lease-collector
    python3 benchmarks/handoff_bench.py --dry-run --kinds sfn,sqs        # the plan, no AWS

Every kind leases the same image (examples/handoff-agent) with the task
{"steps": ["echo bench", "sleep 3"]}. The bench watches from the outside only: the fleet
listing every 0.5 s (when the VM appears and turns RUNNING), the VM's GET /status (when the
lease was accepted and when it finished), and the orchestrator's own terminal state (Step
Functions describe-execution, Lambda get-durable-execution, or the queue message the generic
controller would read). One VM at a time, a settle loop for TERMINATED between runs, and
everything the bench launched is terminated on exit. Emits JSON plus an SVG transcript to
benchmarks/results/handoff-<timestamp>.json/.svg like capture_demos.py.

`--durable-function` must be a durable-handoff orchestrator deployed with
ImageName=handoff-agent: its event `{"task": {...}}` passes the task straight to the lease.
"""

from __future__ import annotations

import argparse
import calendar
import json
import secrets
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

RESULTS = Path(__file__).parent / "results"
KINDS = ("sfn", "durable", "sqs", "eventbridge", "http")
GENERIC = ("sqs", "eventbridge", "http")
TASK = {"steps": ["echo bench", "sleep 3"]}
POLL_S = 0.5
SETTLE_S = 120
# published Lambda MicroVM rates for the 2 GB tier: memory per GB-second plus two vCPU-seconds
VM_USD_PER_S = 0.0000276944 + 2 * 0.0000036667
# what the orchestrator itself bills per lease: 4 Standard transitions, 3 durable operations, 0 for a poller
ORCHESTRATOR_USD = {"sfn": 4 * 0.000025, "durable": 3 * 0.000008, "sqs": 0.0, "eventbridge": 0.0, "http": 0.0}
REQUIRED = {
    "sfn": ["sfn_arn"], "durable": ["durable_function"], "sqs": ["queue_url"],
    "eventbridge": ["bus", "collector_queue_url"], "http": ["http_target", "collector_queue_url"],
}
TERMINAL_SFN = {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"}
TERMINAL_DURABLE = {"SUCCEEDED", "FAILED", "TIMED_OUT", "STOPPED"}


def pctile(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else None


def iso_utc_to_epoch(s: str) -> float:
    """The hook runtime's `started` is "%Y-%m-%dT%H:%M:%SZ" (second resolution)."""
    return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))


@dataclass
class Run:
    kind: str
    index: int
    name: str
    t0: float = 0.0
    vm_id: str | None = None
    t_seen: float | None = None
    t_running: float | None = None
    t_lease: float | None = None          # VM clock: job `started` once the lease shows in /status
    work_s: float | None = None           # VM clock: elapsed_s when lease.done was first seen
    t_vm_done: float | None = None
    t_orch_done: float | None = None
    t_terminated: float | None = None
    orchestrator_status: str | None = None
    vm_error: dict | None = None
    notes: list = field(default_factory=list)

    def metrics(self, max_duration: int) -> dict:
        end = self.t_terminated or time.time()
        vm_seconds = min(end - self.t_running, max_duration) if self.t_running else None
        m = {
            "launch_to_lease_s": self.t_lease - self.t0 if self.t_lease else None,
            "work_s": self.work_s,
            "completion_to_resume_s": (self.t_orch_done - self.t_vm_done
                                       if self.t_orch_done and self.t_vm_done else None),
            "end_to_end_s": self.t_orch_done - self.t0 if self.t_orch_done else None,
            "vm_seconds": vm_seconds,
            "cost_usd": (vm_seconds * VM_USD_PER_S + ORCHESTRATOR_USD[self.kind]) if vm_seconds else None,
        }
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in m.items()}


# ------------------------------------------------------------------------------- the bench
class Bench:
    def __init__(self, args, console: Console):
        import boto3
        from microvm import FleetManager, PlaneConfig
        from microvm.lease import LeasePolicy

        self.args, self.console = args, console
        self.cfg = PlaneConfig()
        self.fm = FleetManager(self.cfg)
        self.session = boto3.Session(profile_name=self.cfg.profile, region_name=self.cfg.region)
        self.policy = LeasePolicy(budget_s=args.budget, heartbeat_timeout_s=90, slack_s=120)
        self.launched: set[str] = set()
        self._clients: dict = {}

    def client(self, service):
        if service not in self._clients:
            self._clients[service] = self.session.client(service)
        return self._clients[service]

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

    # -- is the orchestrator done? ----------------------------------------------------------
    def orchestrator_done(self, run: Run, handle: dict) -> str | None:
        if run.kind == "sfn":
            status = self.client("stepfunctions").describe_execution(
                executionArn=handle["execution_arn"])["status"]
            return status if status in TERMINAL_SFN else None
        if run.kind == "durable":
            lam = self.client("lambda")
            if not handle.get("execution_arn"):
                name, qualifier = (self.args.durable_function.split(":") + [None])[:2]
                kw = {"FunctionName": name, "DurableExecutionName": run.name}
                if qualifier:
                    kw["Qualifier"] = qualifier
                items = lam.list_durable_executions_by_function(**kw).get("DurableExecutions", [])
                if not items:
                    return None
                handle["execution_arn"] = items[0]["DurableExecutionArn"]
            status = lam.get_durable_execution(DurableExecutionArn=handle["execution_arn"])["Status"]
            return status if status in TERMINAL_DURABLE else None
        return self._poll_queue(run, handle)

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
                if status == "failure":
                    run.vm_error = body.get("error")
        return outcome

    # -- watching the VM ---------------------------------------------------------------------
    def find_vm(self, run: Run, before: set[str]) -> None:
        for vm in self.fm.list(self.args.image):
            if vm.microvm_id not in before and vm.state != "TERMINATED":
                run.vm_id, run.t_seen = vm.microvm_id, time.time()
                self.launched.add(vm.microvm_id)
                return

    def vm_state(self, run: Run) -> str:
        return self.fm.get(run.vm_id).state

    def status_client(self, run: Run):
        from microvm import EndpointClient

        vm = self.fm.get(run.vm_id)
        return EndpointClient(self.cfg, run.vm_id, endpoint=vm.endpoint)

    def sample_status(self, run: Run, client) -> None:
        try:
            snap = client.status()
        except Exception:                         # not serving yet, or auth token still minting
            return
        lease = snap.get("lease")
        if lease and run.t_lease is None and snap.get("started"):
            run.t_lease = iso_utc_to_epoch(snap["started"])
        if lease and lease.get("done") and run.t_vm_done is None:
            run.t_vm_done, run.work_s = time.time(), snap.get("elapsed_s")
            if lease.get("error"):
                run.vm_error = lease["error"]

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
                if self.vm_state(run) == "RUNNING":
                    run.t_running = time.time()
                    c.print(f"    vm {run.vm_id} RUNNING at +{run.t_running - run.t0:.1f}s")
            else:
                if client is None:
                    client = self.status_client(run)
                if run.t_vm_done is None:
                    self.sample_status(run, client)
                    if run.t_vm_done:
                        c.print(f"    lease accepted at +{(run.t_lease or 0) - run.t0:.1f}s, "
                                f"done after {run.work_s}s of work (+{run.t_vm_done - run.t0:.1f}s)")
            status = self.orchestrator_done(run, handle)
            if status:
                run.t_orch_done, run.orchestrator_status = time.time(), status
                c.print(f"    orchestrator {status} at +{run.t_orch_done - run.t0:.1f}s")
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

    def settle(self, run: Run) -> None:
        """Wait for the VM to be TERMINATED (also the memory quota clearing for the next run)."""
        if not run.vm_id:
            return
        end = time.time() + SETTLE_S
        while time.time() < end:
            if self.vm_state(run) == "TERMINATED":
                run.t_terminated = time.time()
                self.launched.discard(run.vm_id)
                return
            time.sleep(2)
        run.notes.append(f"{run.vm_id} not TERMINATED after {SETTLE_S}s settle")

    def cleanup(self) -> None:
        for vm_id in list(self.launched):
            try:
                if self.fm.get(vm_id).state != "TERMINATED":
                    self.fm.terminate(vm_id)
                    self.console.print(f"[dim]terminated leftover {vm_id}[/]")
            except Exception as e:
                self.console.print(f"[red]could not terminate {vm_id}: {e}[/]")


# ------------------------------------------------------------------------------- reporting
def summarise(runs: list[Run], max_duration: int) -> dict:
    out: dict = {}
    for kind in KINDS:
        rows = [r.metrics(max_duration) for r in runs if r.kind == kind]
        if not rows:
            continue
        agg = {}
        for key in rows[0]:
            xs = [r[key] for r in rows if r[key] is not None]
            agg[key] = {"p50": round(statistics.median(xs), 3), "min": round(min(xs), 3),
                        "max": round(max(xs), 3), "n": len(xs)} if xs else None
        out[kind] = {"runs": len(rows), "ok": sum(1 for r in runs if r.kind == kind and
                                                r.orchestrator_status == "SUCCEEDED"), **agg}
    return out


def table(summary: dict) -> Table:
    t = Table(title="lease handoff, p50 per kind", header_style="bold magenta")
    for col in ("kind", "ok", "launch to lease", "work", "completion to resume", "end to end", "VM s",
                "cost / lease"):
        t.add_column(col, justify="right" if col != "kind" else "left")

    def cell(agg, key, fmt="{:.1f}s"):
        v = (agg.get(key) or {}).get("p50")
        return fmt.format(v) if v is not None else "-"

    for kind, agg in summary.items():
        t.add_row(kind, f"{agg['ok']}/{agg['runs']}", cell(agg, "launch_to_lease_s"), cell(agg, "work_s"),
                  cell(agg, "completion_to_resume_s"), cell(agg, "end_to_end_s"),
                  cell(agg, "vm_seconds", "{:.0f}"), cell(agg, "cost_usd", "${:.5f}"))
    return t


def summarize_files(paths: list[str]) -> None:
    """Merge the per-kind result files of one session into a single table, JSON, and SVG."""
    from pathlib import Path
    merged: dict = {}
    files = [Path(p) for p in paths] or sorted(Path("benchmarks/results").glob("handoff-*.json"))
    for path in files:
        data = json.loads(path.read_text())
        for run in data["runs"]:
            metrics = dict(run["metrics"])
            if metrics.get("vm_seconds") is not None:   # recompute: early files rounded the cost away
                metrics["cost_usd"] = round(
                    metrics["vm_seconds"] * VM_USD_PER_S + ORCHESTRATOR_USD[run["kind"]], 6)
            merged.setdefault(run["kind"], []).append({**run, "metrics": metrics})
    summary = {}
    for kind in KINDS:
        rows = merged.get(kind)
        if not rows:
            continue
        agg = {}
        for key in rows[0]["metrics"]:
            xs = [r["metrics"][key] for r in rows if r["metrics"][key] is not None]
            nd = 6 if key == "cost_usd" else 3
            agg[key] = ({"p50": round(statistics.median(xs), nd), "min": round(min(xs), nd),
                         "max": round(max(xs), nd), "n": len(xs)} if xs else None)
        ok = sum(r["orchestrator_status"] == "SUCCEEDED" for r in rows)
        summary[kind] = {"runs": len(rows), "ok": ok, **agg}
    console = Console(record=True, width=110)
    console.rule("[bold]lease handoff bench: all kinds, one image, one account")
    console.print(table(summary))
    console.print(f"cost model: VM seconds x ${VM_USD_PER_S:.10f} (2 GB tier) + orchestrator ops "
                  f"{ORCHESTRATOR_USD}; files: {', '.join(p.name for p in files)}")
    out_dir = Path("benchmarks/results/handoff")
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"summary": summary, "sources": [p.name for p in files],
               "rates": {"vm_usd_per_s": VM_USD_PER_S, "orchestrator_usd": ORCHESTRATOR_USD}}
    (out_dir / "summary.json").write_text(json.dumps(payload, indent=2))
    console.save_svg(str(out_dir / "summary.svg"), title="mvm lease handoff bench")
    console.print(f"summary -> {out_dir / 'summary.json'} (+ .svg)")


def plan(args, kinds: list[str], console: Console) -> None:
    console.rule("[bold]handoff bench: dry run")
    console.print(f"image [cyan]{args.image}[/], task {json.dumps(TASK)}, {args.runs} run(s) per kind, "
                  f"budget {args.budget}s, one VM at a time, {SETTLE_S}s settle for TERMINATED between runs")
    how = {
        "sfn": "start_execution on --sfn-arn; done when describe-execution leaves RUNNING",
        "durable": "lambda.invoke (Event, DurableExecutionName) on --durable-function; done when "
                   "get-durable-execution is terminal",
        "sqs": "FleetManager.lease(kind=sqs, target=--queue-url); done on the success/failure message",
        "eventbridge": "FleetManager.lease(kind=eventbridge, target=--bus); done when the rule delivers the "
                       "success/failure event to --collector-queue-url",
        "http": "FleetManager.lease(kind=http, target=--http-target); done when the collector appends the "
                "success/failure POST to --collector-queue-url",
    }
    for kind in kinds:
        missing = [f"--{a.replace('_', '-')}" for a in REQUIRED[kind] if not getattr(args, a)]
        flag = f"[red]missing {', '.join(missing)}[/]" if missing else "[green]args ok[/]"
        console.print(f"  [cyan]{kind:<11}[/] {how[kind]}  {flag}")
    console.print("per run: fm.list every 0.5 s for the new VM and RUNNING; GET /status for `started` (lease "
                  "accepted) and lease.done; the orchestrator's terminal state; then wait for TERMINATED.")
    console.print(f"cost model: VM seconds x ${VM_USD_PER_S:.10f} (2 GB tier) + orchestrator ops "
                  f"{ {k: round(v, 6) for k, v in ORCHESTRATOR_USD.items()} }")
    console.print(f"output: {RESULTS}/handoff-<timestamp>.json and .svg. Nothing was launched.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--kinds", default=",".join(KINDS), help=f"comma list from {','.join(KINDS)}")
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
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit; touches nothing")
    ap.add_argument("--summarize", nargs="*", metavar="RESULT_JSON",
                    help="merge earlier result files into one table, JSON, and SVG; launches nothing")
    args = ap.parse_args(argv)
    if args.summarize is not None:
        summarize_files(args.summarize)
        return 0

    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        ap.error(f"unknown kinds {bad}; choose from {', '.join(KINDS)}")
    console = Console(record=True, width=110)
    if args.dry_run:
        plan(args, kinds, console)
        return 0
    for kind in kinds:
        missing = [f"--{a.replace('_', '-')}" for a in REQUIRED[kind] if not getattr(args, a)]
        if missing:
            ap.error(f"kind {kind} needs {', '.join(missing)}")

    bench = Bench(args, console)
    console.rule(f"[bold]lease handoff bench: {', '.join(kinds)} x {args.runs} on {args.image}")
    runs: list[Run] = []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        for kind in kinds:
            for i in range(args.runs):
                # never more than one VM: wait for anything live on this image to clear first
                end = time.time() + SETTLE_S
                while time.time() < end and any(v.state != "TERMINATED" for v in bench.fm.list(args.image)):
                    time.sleep(5)
                run = Run(kind=kind, index=i, name=f"bench-{kind}-{stamp}-{i}")
                try:
                    runs.append(bench.one(run))
                except Exception as e:
                    run.notes.append(f"failed: {type(e).__name__}: {e}")
                    console.print(f"    [red]{run.name} failed: {e}[/]")
                    runs.append(run)
                    bench.settle(run)
    finally:
        bench.cleanup()
        summary = summarise(runs, bench.policy.max_duration())
        console.print(table(summary))
        RESULTS.mkdir(exist_ok=True)
        out = {"date": time.strftime("%Y-%m-%d"), "region": bench.cfg.region, "image": args.image,
               "task": TASK, "budget_s": args.budget,
               "rates": {"vm_usd_per_s": VM_USD_PER_S, "orchestrator_usd": ORCHESTRATOR_USD},
               "summary": summary,
               "runs": [{**asdict(r), "metrics": r.metrics(bench.policy.max_duration())} for r in runs]}
        (RESULTS / f"handoff-{stamp}.json").write_text(json.dumps(out, indent=2, default=str))
        console.save_svg(str(RESULTS / f"handoff-{stamp}.svg"), title="awesome-microvm - lease handoff bench")
        console.print(f"[dim]results -> benchmarks/results/handoff-{stamp}.json (+ .svg)[/]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
