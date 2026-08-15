"""Eval harness: fan a task suite across a fleet of pristine eval workers.

    python3 harness.py --image agent-eval --workers 5 --tasks tasks.json

Launches N identical VMs with Fleet.scale_to, round-robins tasks across
them (each /evaluate wipes its workspace first), prints a scoreboard, and
drains the fleet. Every score comes from a byte-identical environment.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import time

from microvm import EndpointClient, Fleet, FleetManager, PlaneConfig
from microvm.fleet import IdlePolicy

from rich.console import Console
from rich.table import Table

console = Console()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="agent-eval")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--tasks", default="tasks.json")
    ap.add_argument("--keep-fleet", action="store_true")
    args = ap.parse_args()

    cfg = PlaneConfig()
    tasks = json.load(open(args.tasks))
    fleet = Fleet(
        FleetManager(cfg), args.image,
        idle_policy=IdlePolicy(max_idle=600, suspended_for=60, auto_resume=False),
        max_duration=3600,  # eval fleets are disposable — hard cap their lifetime
    )

    console.print(f"[bold]scaling eval fleet to {args.workers} workers…[/]")
    t0 = time.time()
    fleet.scale_to(args.workers, wait_running=True)
    members = fleet.members()
    console.print(f"[green]✓[/] {len(members)} pristine environments in {time.time()-t0:.1f}s")

    clients = [EndpointClient(cfg, vm.microvm_id) for vm in members]
    for c in clients:
        c.wait_ready()

    def run_task(i_task):
        i, task = i_task
        client = clients[i % len(clients)]
        resp = client.post("/evaluate", json=task, timeout=180)
        return task["task_id"], resp.json() if resp.ok else {"passed": False, "error": resp.text}

    t1 = time.time()
    with futures.ThreadPoolExecutor(max_workers=len(clients)) as pool:
        results = dict(pool.map(run_task, enumerate(tasks)))
    wall = time.time() - t1

    table = Table(title=f"agent eval — {len(tasks)} tasks / {len(clients)} workers / {wall:.1f}s wall",
                  header_style="bold magenta")
    for col in ("task", "worker", "passed", "summary"):
        table.add_column(col)
    passed = 0
    for tid, r in results.items():
        ok = r.get("passed", False)
        passed += ok
        table.add_row(str(tid), str(r.get("worker", "-")),
                      "[green]PASS[/]" if ok else "[red]FAIL[/]",
                      str(r.get("summary", r.get("error", "")))[:60])
    console.print(table)
    console.print(f"[bold]score: {passed}/{len(tasks)} ({100*passed/len(tasks):.0f}%)[/]")

    if not args.keep_fleet:
        n = fleet.drain()
        console.print(f"[dim]drained {n} workers[/]")


if __name__ == "__main__":
    main()
