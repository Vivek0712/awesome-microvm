// Live scenarios against a deployed Web Function. Writes results/live.json.
//
//   WEB_URL=https://<id>.lambda-web.us-east-1.on.aws DEMO_KEY=... node test/live.mjs [scenario ...]
import { mkdirSync, writeFileSync } from "node:fs";

const U = process.env.WEB_URL, KEY = process.env.DEMO_KEY;
const H = { "content-type": "application/json", "x-demo-key": KEY };
const sleep = (ms) => new Promise((ok) => setTimeout(ok, ms));

async function stream(steps) {
  const t0 = Date.now();
  const r = await fetch(`${U}/jobs?stream=1`, { method: "POST", headers: H, body: JSON.stringify({ steps }) });
  if (!r.ok) return { status: r.status, body: await r.json() };
  const dec = new TextDecoder();
  let buf = "", firstLogMs = null, logs = 0, launched, result;
  for await (const chunk of r.body) {
    buf += dec.decode(chunk, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const f = buf.slice(0, i); buf = buf.slice(i + 2);
      const ev = /^event: (.*)$/m.exec(f)?.[1], d = JSON.parse(/^data: (.*)$/m.exec(f)?.[1] || "null");
      if (ev === "launched") launched = d;
      if ((ev === "log" || ev === "snapshot") && firstLogMs === null) firstLogMs = Date.now() - t0;
      if (ev === "log") logs++;
      if (ev === "result") result = d;
    }
  }
  return { status: r.status, launchMs: launched?.launchMs, firstLogMs, totalMs: Date.now() - t0, logs,
           state: result?.state, heartbeats: result?.heartbeats, microvmId: result?.microvmId,
           error: result?.outcome?.error, passed: result?.outcome?.result?.passed };
}

async function poll(id, timeoutS = 180) {
  for (let t = 0; t < timeoutS; t += 2) {
    const j = await (await fetch(`${U}/jobs/${id}`, { headers: H })).json();
    if (["succeeded", "failed", "lost"].includes(j.state)) return j;
    await sleep(2000);
  }
  throw new Error(`job ${id} did not finish`);
}

const scenarios = {
  async stream_success() {
    return stream(["uname -m", "python3 -c \"import os; print(os.cpu_count())\"", "for i in 1 2 3; do echo tick $i; sleep 1; done"]);
  },
  async async_poll() {
    const t0 = Date.now();
    const r = await fetch(`${U}/jobs`, { method: "POST", headers: H, body: JSON.stringify({ steps: ["sleep 4", "echo done"] }) });
    const accepted = await r.json();
    const acceptMs = Date.now() - t0;
    const j = await poll(accepted.id);
    return { status: r.status, acceptMs, totalMs: Date.now() - t0, state: j.state, heartbeats: j.heartbeats,
             passed: j.outcome?.result?.passed, launchMs: j.launchMs };
  },
  async failing_step() {
    return stream(["echo before", "exit 3", "echo never"]);
  },
  async long_job_heartbeats() {
    return stream(["for i in $(seq 1 30); do echo $i; sleep 1; done"]);
  },
  async terminated_mid_job() {
    // an operator (or a janitor) terminates the VM while the job runs: the VM's terminate hook should
    // fail the lease, or, if the VM is gone before it can, the job is reaped as lost
    const { LambdaMicrovmsClient, TerminateMicrovmCommand } = await import("@aws-sdk/client-lambda-microvms");
    const t0 = Date.now();
    const r = await fetch(`${U}/jobs`, { method: "POST", headers: H, body: JSON.stringify({ steps: ["sleep 120"] }) });
    const { id, microvmId } = await r.json();
    await sleep(8000);
    const tt = Date.now();
    await new LambdaMicrovmsClient({ region: "us-east-1" }).send(new TerminateMicrovmCommand({ microvmIdentifier: microvmId }));
    const j = await poll(id, 240);
    return { state: j.state, heartbeats: j.heartbeats, error: j.outcome?.error, afterTerminateMs: Date.now() - tt, totalMs: Date.now() - t0 };
  },
  async four_at_once() {
    const t0 = Date.now();
    const runs = await Promise.all([1, 2, 3, 4].map((n) => stream([`echo job ${n}`, "sleep 3"])));
    return { wallMs: Date.now() - t0, states: runs.map((r) => r.state), launchMs: runs.map((r) => r.launchMs),
             totalMs: runs.map((r) => r.totalMs), vms: new Set(runs.map((r) => r.microvmId)).size };
  },
  async guards() {
    const noKey = await fetch(`${U}/jobs`, { method: "POST", body: JSON.stringify({ steps: ["ls"] }) });
    const bad = await fetch(`${U}/jobs`, { method: "POST", headers: H, body: JSON.stringify({ steps: [] }) });
    const r = await fetch(`${U}/jobs`, { method: "POST", headers: H, body: JSON.stringify({ steps: ["sleep 3"] }) });
    const { id } = await r.json();
    const forged = await fetch(`${U}/callbacks/${id}`, {
      method: "POST", headers: { "content-type": "application/json", authorization: "Bearer forged" },
      body: JSON.stringify({ status: "success", result: { passed: true } }),
    });
    const j = await poll(id);
    return { noKey: noKey.status, emptySteps: bad.status, forgedCallback: forged.status, realOutcome: j.state };
  },
};

const pick = process.argv.slice(2);
const out = {};
for (const [name, fn] of Object.entries(scenarios)) {
  if (pick.length && !pick.includes(name)) continue;
  try { out[name] = await fn(); } catch (e) { out[name] = { error: e.message }; }
  console.log(name, JSON.stringify(out[name]));
}
mkdirSync("results", { recursive: true });
writeFileSync(`results/live-${new Date().toISOString().slice(0, 19).replace(/:/g, "")}.json`,
              JSON.stringify({ url: U, at: new Date().toISOString(), scenarios: out }, null, 2));
