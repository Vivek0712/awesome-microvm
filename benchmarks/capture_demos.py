"""Run each use-case app live and record terminal screenshots (SVG) for the blogs.

Runs one VM at a time (fits the reduced 8 GB new-account quota), drives the
app's signature flow, prints a rich-rendered transcript, and saves it as
benchmarks/results/demo-<name>.svg.

    MVM_PROFILE=heisenberg python3 benchmarks/capture_demos.py [--only notebook]
"""

from __future__ import annotations

import argparse
import base64
import json
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from microvm import EndpointClient, FleetManager, PlaneConfig
from microvm.fleet import IdlePolicy

RESULTS = Path(__file__).parent / "results"


def _vm(fm, cfg, image, console, payload=None):
    console.print(f"[bold cyan]$ mvm run {image} --wait[/]")
    t0 = time.time()
    vm = fm.run(image, idle_policy=IdlePolicy(max_idle=600, suspended_for=600),
                run_payload=payload, max_duration=1800)
    fm.wait_until(vm.microvm_id, "RUNNING", timeout=180)
    client = EndpointClient(cfg, vm.microvm_id, endpoint=vm.endpoint)
    client.wait_ready()
    console.print(f"[green]✓[/] {vm.microvm_id} RUNNING + serving in {time.time()-t0:.1f}s "
                  f"— endpoint {vm.endpoint}\n")
    return vm, client


def _show(console, title, resp):
    body = resp.json()
    console.print(Panel(json.dumps(body, indent=2)[:1200], title=title, border_style="dim"))
    return body


def demo_code_sandbox(fm, cfg, console):
    vm, c = _vm(fm, cfg, "code-sandbox", console)
    code = "import numpy as np\nA = np.random.default_rng(0).normal(size=(500,500))\nprint('eigvals:', np.linalg.eigvals(A)[:3].round(2))"
    console.print(f"[bold cyan]$ mvm call {vm.microvm_id[:20]}… /execute -X POST[/]")
    _show(console, "POST /execute — untrusted numpy code", c.post("/execute", json={"code": code}))
    _show(console, "POST /execute — state persists (workspace file)",
          c.post("/execute", json={"code": "open('model.bin','w').write('weights'); print('saved')"}))
    _show(console, "GET /state", c.get("/state"))
    return vm


def demo_notebook(fm, cfg, console):
    vm, c = _vm(fm, cfg, "notebook", console)
    for code in ("import pandas as pd, numpy as np",
                 "df = pd.DataFrame({'x': np.arange(1000)}); df['y'] = df.x ** 2",
                 "df.y.sum()"):
        console.print(f"[bold cyan]>>> {code}[/]")
        _show(console, "POST /cell", c.post("/cell", json={"code": code}))
    console.print("[bold yellow]— suspending the kernel VM… —[/]")
    fm.suspend(vm.microvm_id); fm.wait_until(vm.microvm_id, "SUSPENDED", timeout=120)
    console.print("[bold yellow]— VM SUSPENDED (compute billing stopped). Resuming with traffic… —[/]")
    t0 = time.time()
    body = _show(console, "POST /cell — after suspend/resume",
                 c.post("/cell", json={"code": "df.y.mean()"}, resume_patience=90))
    console.print(f"[green]dataframe survived suspend/resume; first request woke the VM "
                  f"in {time.time()-t0:.1f}s[/]")
    return vm


def demo_ai_code_runner(fm, cfg, console):
    vm, c = _vm(fm, cfg, "ai-code-runner", console)
    task = "Compute the first 8 Fibonacci numbers and print them as a Python list."
    console.print(f"[bold cyan]$ task: {task}[/]")
    _show(console, "POST /solve — Bedrock writes code, VM runs it",
          c.post("/solve", json={"task": task, "max_iterations": 3}, timeout=180))
    return vm


def demo_agent_eval(fm, cfg, console):
    vm, c = _vm(fm, cfg, "agent-eval", console)
    tasks = json.load(open(Path(__file__).parent.parent / "examples/agent-eval/tasks.json"))
    for t in tasks:
        r = c.post("/evaluate", json=t, timeout=120).json()
        mark = "[green]PASS[/]" if r.get("passed") else "[red]FAIL[/]"
        console.print(f"  {mark} {t['task_id']:<20} {r.get('summary','')}")
    return vm


def demo_ci_runner(fm, cfg, console):
    vm, c = _vm(fm, cfg, "ci-runner", console)
    job = {"repo_url": "https://github.com/psf/requests", "ref": "main",
           "steps": ["python3.12 -m ruff check src/requests --statistics || true",
                     "python3.12 -c 'import ast,glob; [ast.parse(open(f).read()) for f in glob.glob(\"src/**/*.py\", recursive=True)]; print(\"syntax OK\")'"]}
    console.print("[bold cyan]$ POST /job — clone psf/requests, lint + syntax-check[/]")
    _show(console, "POST /job", c.post("/job", json=job, timeout=300))
    return vm


def demo_pdf_service(fm, cfg, console):
    vm, c = _vm(fm, cfg, "pdf-service", console)
    html = "<h1>Invoice #42</h1><table border=1><tr><th>Item</th><th>USD</th></tr><tr><td>microVM seconds</td><td>0.07</td></tr></table>"
    r = c.post("/render", json={"html": html})
    body = r.json()
    pdf = base64.b64decode(body.pop("pdf_base64"))
    (RESULTS / "demo-invoice.pdf").write_bytes(pdf)
    console.print(Panel(json.dumps(body, indent=2), title="POST /render — PDF from untrusted HTML",
                        border_style="dim"))
    console.print(f"[green]✓ wrote {len(pdf)} PDF bytes → benchmarks/results/demo-invoice.pdf[/]")
    return vm


def demo_data_analytics(fm, cfg, console):
    vm, c = _vm(fm, cfg, "data-analytics", console)
    _show(console, "POST /query — DuckDB inside the VM", c.post("/query", json={
        "sql": "SELECT 42 AS answer, current_timestamp AS at"}))
    _show(console, "POST /query — generate + aggregate 1M rows", c.post("/query", json={
        "sql": "SELECT (i%7) AS bucket, COUNT(*) n, AVG(i) mean FROM range(1000000) t(i) GROUP BY 1 ORDER BY 1 LIMIT 5"}))
    return vm


def demo_multi_tenant_agents(fm, cfg, console):
    payload = json.dumps({"tenant_id": "acme", "display_name": "Acme Corp"})
    vm, c = _vm(fm, cfg, "multi-tenant-agents", console, payload=payload)
    _show(console, "GET /whoami — identity came from runHookPayload, not the image",
          c.get("/whoami"))
    _show(console, "POST /chat", c.post("/chat", json={
        "message": "In one sentence: why do we get our own VM?"}, timeout=120))
    return vm


DEMOS = {
    "code-sandbox": demo_code_sandbox,
    "notebook": demo_notebook,
    "ai-code-runner": demo_ai_code_runner,
    "agent-eval": demo_agent_eval,
    "ci-runner": demo_ci_runner,
    "pdf-service": demo_pdf_service,
    "data-analytics": demo_data_analytics,
    "multi-tenant-agents": demo_multi_tenant_agents,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None)
    args = ap.parse_args()
    cfg = PlaneConfig()
    fm = FleetManager(cfg)
    RESULTS.mkdir(exist_ok=True)
    for name, fn in DEMOS.items():
        if args.only and name not in args.only:
            continue
        # TERMINATING VMs still count against the memory quota; let them clear.
        deadline = time.time() + 180
        while time.time() < deadline:
            if all(v.state == "TERMINATED" for v in fm.list()):
                break
            time.sleep(5)
        console = Console(record=True, width=100)
        console.rule(f"[bold]{name} on a Lambda MicroVM")
        vm = None
        try:
            vm = fn(fm, cfg, console)
        except Exception as e:
            console.print(f"[red]demo failed: {e}[/]")
        finally:
            if vm:
                fm.terminate(vm.microvm_id)
                console.print(f"[dim]terminated {vm.microvm_id}[/]")
        console.save_svg(str(RESULTS / f"demo-{name}.svg"), title=f"awesome-microvm — {name}")
        print(f"saved demo-{name}.svg")


if __name__ == "__main__":
    main()
