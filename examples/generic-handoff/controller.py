"""Lease a microVM from a laptop and wait for it over SQS, EventBridge, or an HTTP collector.

    python3 controller.py --image handoff-agent --kind sqs [--task JSON] [--budget 300] [--runs N]
    python3 controller.py --image handoff-agent --kind eventbridge
    python3 controller.py --image handoff-agent --kind http --target https://<collector-url>/ [--queue-url URL]

Every kind ends in one SQS queue the controller long-polls: `sqs` has the VM write to it
directly, `eventbridge` routes `microvm.lease` events from a bus into it, `http` has the
collector Lambda (collector/template.yaml) append each POST to it. The loop is the same for
all three: launch with FleetManager.lease, print each heartbeat/success/failure as it lands,
terminate the VM, enforce --budget client-side, and print a summary table plus a JSON file.
"""

from __future__ import annotations

import argparse
import json
import secrets
import statistics
import sys
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from microvm import FleetManager, Lease, LeasePolicy, PlaneConfig
from rich.console import Console
from rich.table import Table

BUS = "microvm-lease"
RULE = "microvm-lease-to-sqs"
DEFAULT_TASK = {"steps": ["echo hello from the lease", "python3 -c \"print(2+2)\"", "sleep 2"]}
console = Console()


# -- plumbing: one queue per kind, created idempotently ---------------------------------------
def queue_for(sqs, name: str) -> tuple[str, str]:
    url = sqs.create_queue(QueueName=name)["QueueUrl"]  # idempotent for a standard queue
    arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    return url, arn


def setup_sqs(session) -> tuple[str, str, str]:
    """Returns (queue_url, lease_target, grant): the VM sends straight to the queue."""
    url, arn = queue_for(session.client("sqs"), "microvm-lease-sqs")
    return url, url, f"sqs:SendMessage on {arn}  (mvm lease policy --kind sqs --orchestrator {arn})"


def setup_eventbridge(session) -> tuple[str, str, str]:
    """Bus + rule matching source microvm.lease, targeting the queue; the queue lets events.amazonaws.com in."""
    sqs, events = session.client("sqs"), session.client("events")
    url, arn = queue_for(sqs, "microvm-lease-eventbridge")
    try:
        bus_arn = events.create_event_bus(Name=BUS)["EventBusArn"]
    except events.exceptions.ResourceAlreadyExistsException:
        bus_arn = events.describe_event_bus(Name=BUS)["Arn"]
    rule_arn = events.put_rule(Name=RULE, EventBusName=BUS, State="ENABLED",
                               EventPattern=json.dumps({"source": ["microvm.lease"]}))["RuleArn"]
    policy = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "events.amazonaws.com"}, "Action": "sqs:SendMessage",
        "Resource": arn, "Condition": {"ArnEquals": {"aws:SourceArn": rule_arn}}}]}
    sqs.set_queue_attributes(QueueUrl=url, Attributes={"Policy": json.dumps(policy)})
    events.put_targets(Rule=RULE, EventBusName=BUS, Targets=[{"Id": "queue", "Arn": arn}])
    return url, BUS, f"events:PutEvents on {bus_arn}  (mvm lease policy --kind eventbridge --orchestrator {bus_arn})"


def setup_http(session, target: str | None, queue_url: str | None, stack: str) -> tuple[str, str, str]:
    """Nothing to create: the collector stack owns the queue; read its outputs when not given."""
    if not (target and queue_url):
        outs = session.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0].get("Outputs", [])
        outputs = {o["OutputKey"]: o["OutputValue"] for o in outs}
        target = target or outputs.get("CollectorUrl")
        queue_url = queue_url or outputs.get("QueueUrl")
    if not (target and queue_url):
        sys.exit("--kind http needs --target and --queue-url, or a deployed collector stack (--stack)")
    return queue_url, target, "no IAM: the VM POSTs to the collector's Function URL with the bearer token"


def unwrap(body: str) -> dict:
    """Normalize a queue message to the VM's payload: EventBridge wraps it in an event envelope."""
    msg = json.loads(body)
    if "detail-type" in msg and isinstance(msg.get("detail"), dict):
        msg = msg["detail"]
    return msg


# -- the loop -----------------------------------------------------------------------------
def one_run(fm: FleetManager, sqs, args, queue_url: str, target: str, index: int) -> dict:
    token = secrets.token_urlsafe(24)
    lease = Lease(kind=args.kind, token=token, region=fm.cfg.region, target=target,
                  heartbeat_s=args.heartbeat, id=f"{args.kind}-{index}-{int(time.time())}")
    t0 = time.time()
    vm = fm.lease(args.image, lease, args.task, LeasePolicy(budget_s=args.budget))
    rec = {"run": index, "lease_id": lease.id, "microvm_id": vm.microvm_id, "status": "timed_out", "heartbeats": 0,
           "launch_to_first_message_s": None, "work_s": None, "total_s": None, "error": None, "result": None}

    def stamp() -> str:
        return f"[dim]{time.strftime('%H:%M:%S')} +{time.time() - t0:6.1f}s[/]"

    console.print(f"{stamp()} launched [bold]{vm.microvm_id}[/] lease={lease.id} kind={args.kind}")
    deadline = t0 + args.budget
    while time.time() < deadline and rec["status"] == "timed_out":
        wait = int(max(1, min(10, deadline - time.time())))
        for m in sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10,
                                     WaitTimeSeconds=wait).get("Messages", []):
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=m["ReceiptHandle"])
            try:
                msg = unwrap(m["Body"])
            except ValueError:
                continue
            if msg.get("token") != token:
                console.print(f"{stamp()} [dim]ignored a message for another lease[/]")
                continue
            if rec["launch_to_first_message_s"] is None:
                rec["launch_to_first_message_s"] = round(time.time() - t0, 3)
            status = msg.get("status")
            if status == "heartbeat":
                rec["heartbeats"] += 1
                console.print(f"{stamp()} heartbeat #{rec['heartbeats']} vm_elapsed={msg.get('elapsed_s')}s")
                continue
            rec.update(status=status, work_s=msg.get("elapsed_s"), total_s=round(time.time() - t0, 3),
                       result=msg.get("result"), error=msg.get("error"))
            if status == "success":
                console.print(f"{stamp()} [green]success[/] {json.dumps(msg.get('result'))[:300]}")
            else:
                err = msg.get("error") or {}
                console.print(f"{stamp()} [red]failure[/] {err.get('error_type')}: {err.get('message')} "
                              f"retryable={err.get('retryable')}")
            break
    if rec["status"] == "timed_out":
        rec["total_s"] = round(time.time() - t0, 3)
        console.print(f"{stamp()} [yellow]timed_out[/] no completion within the {args.budget} s budget")
    try:
        fm.terminate(vm.microvm_id)
        console.print(f"{stamp()} terminated {vm.microvm_id}")
    except (ClientError, BotoCoreError) as e:  # already gone (duration cap, janitor): fine
        console.print(f"{stamp()} terminate {vm.microvm_id}: {type(e).__name__}: {e}")
    return rec


def p50(values: list) -> float | None:
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.median(vals), 3) if vals else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", default="handoff-agent")
    ap.add_argument("--kind", required=True, choices=["sqs", "eventbridge", "http"])
    ap.add_argument("--target", help="http: the collector's Function URL")
    ap.add_argument("--queue-url", help="http: the collector's queue (default: read from the stack outputs)")
    ap.add_argument("--stack", default="microvm-lease-collector", help="http: the collector stack name")
    ap.add_argument("--task", type=json.loads, default=DEFAULT_TASK, help="task JSON for the agent")
    ap.add_argument("--budget", type=int, default=300, help="client-side timeout per run, seconds")
    ap.add_argument("--heartbeat", type=int, default=30, help="heartbeat interval requested from the VM")
    ap.add_argument("--runs", type=int, default=1)
    args = ap.parse_args()

    cfg = PlaneConfig()
    fm = FleetManager(cfg)
    session = boto3.Session(region_name=cfg.region, profile_name=cfg.profile)
    if args.kind == "sqs":
        queue_url, target, grant = setup_sqs(session)
    elif args.kind == "eventbridge":
        queue_url, target, grant = setup_eventbridge(session)
    else:
        queue_url, target, grant = setup_http(session, args.target, args.queue_url, args.stack)
    console.print(f"[bold]{args.kind}[/] queue={queue_url}\ntarget={target}\nVM role needs: {grant}\n")

    sqs = session.client("sqs")
    runs = [one_run(fm, sqs, args, queue_url, target, i + 1) for i in range(args.runs)]

    table = Table(title=f"{args.kind}: {len(runs)} run(s), budget {args.budget} s")
    for col in ("run", "vm", "status", "heartbeats", "launch_to_first_message_s", "work_s", "total_s"):
        table.add_column(col, justify="right" if col.endswith("_s") or col == "heartbeats" else "left")
    for r in runs:
        table.add_row(str(r["run"]), r["microvm_id"], r["status"], str(r["heartbeats"]),
                      *(str(r[k]) for k in ("launch_to_first_message_s", "work_s", "total_s")))
    summary = {k: p50([r[k] for r in runs]) for k in ("launch_to_first_message_s", "work_s", "total_s")}
    table.add_row("p50", "", "", "", *(str(summary[k]) for k in summary), style="bold")
    console.print(table)
    out = f"results-{args.kind}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    with open(out, "w") as f:
        json.dump({"kind": args.kind, "image": args.image, "task": args.task, "budget_s": args.budget,
                   "region": cfg.region, "runs": runs, "p50": summary}, f, indent=1, default=str)
    console.print(f"wrote {out}")


if __name__ == "__main__":
    main()
