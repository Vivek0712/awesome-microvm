// Unit tests for the Web Function, with in-memory stand-ins for DynamoDB and the MicroVMs API.
import assert from "node:assert/strict";
import { test, before, after, beforeEach } from "node:test";

process.env.NO_LISTEN = "1";
process.env.MICROVM_IMAGE_ARN = "arn:aws:lambda:us-east-1:111122223333:microvm-image:handoff-agent-small";
process.env.JOBS_TABLE = "jobs";
process.env.DEMO_KEY = "k3y";
process.env.PUBLIC_URL = "https://web.example";
process.env.HEARTBEAT_S = "1";
process.env.LAUNCHES_PER_HOUR = "3";

const { server, deps, leasePayload } = await import("../src/server.mjs");

// -- fakes -------------------------------------------------------------------------------------
const items = new Map();
const vmCalls = [];
const condFail = () => Object.assign(new Error("conditional"), { name: "ConditionalCheckFailedException" });

deps.db = {
  async send(cmd) {
    const name = cmd.constructor.name, i = cmd.input;
    if (name === "PutCommand") { items.set(i.Item.id, structuredClone(i.Item)); return {}; }
    if (name === "GetCommand") { const it = items.get(i.Key.id); return { Item: it && structuredClone(it) }; }
    if (name !== "UpdateCommand") throw new Error(`unexpected ${name}`);
    const id = i.Key.id, cur = items.get(id), v = i.ExpressionAttributeValues || {}, e = i.UpdateExpression;
    if (e.startsWith("ADD n")) {                                             // launch budget
      if (cur && cur.n >= v[":cap"]) throw condFail();
      items.set(id, { id, n: (cur?.n || 0) + 1 });
      return {};
    }
    if (e.startsWith("ADD heartbeats")) { cur.heartbeats += 1; cur.lastHeartbeat = v[":t"]; return {}; }
    if (e.startsWith("SET microvmId")) { Object.assign(cur, { microvmId: v[":vm"], endpoint: v[":ep"], launchMs: v[":ms"] }); return {}; }
    if (e === "SET #s = :r") { if (cur.state !== "launching") throw condFail(); cur.state = "running"; return {}; }
    if (e.startsWith("SET #s = :s, finished")) {
      if (!cur || ["succeeded", "failed", "lost"].includes(cur.state)) throw condFail();
      Object.assign(cur, { state: v[":s"], finished: v[":f"], outcome: v[":o"] });
      return { Attributes: structuredClone(cur) };
    }
    throw new Error(`unexpected update ${e}`);
  },
};
deps.vms = {
  async send(cmd) {
    vmCalls.push({ name: cmd.constructor.name, input: cmd.input });
    if (cmd.constructor.name === "RunMicrovmCommand") return { microvmId: "microvm-1", endpoint: "vm1.example" };
    return {};
  },
};

let base;
before(async () => {
  await new Promise((ok) => server.listen(0, "127.0.0.1", ok));
  base = `http://127.0.0.1:${server.address().port}`;
});
after(() => server.close());
beforeEach(() => { items.clear(); vmCalls.length = 0; });

const post = (path, body, headers = {}) => fetch(base + path, {
  method: "POST", headers: { "content-type": "application/json", ...headers }, body: JSON.stringify(body),
});
const startJob = async () => {
  const r = await post("/jobs", { steps: ["echo hi"] }, { "x-demo-key": "k3y" });
  assert.equal(r.status, 202);
  const job = await r.json();
  const payload = JSON.parse(vmCalls.find((c) => c.name === "RunMicrovmCommand").input.runHookPayload);
  return { job, token: payload.lease.token, payload };
};

// -- tests -------------------------------------------------------------------------------------
test("the lease payload is the microvm-ctl http lease, pointing back at this function", () => {
  const p = JSON.parse(leasePayload({ id: "j1", token: "t", target: "https://web.example/callbacks/j1", task: { steps: ["ls"] } }));
  assert.deepEqual(p.lease, { kind: "http", token: "t", region: "us-east-1", target: "https://web.example/callbacks/j1", heartbeat_s: 1, id: "j1" });
  assert.deepEqual(p.task, { steps: ["ls"] });
  assert.throws(() => leasePayload({ id: "j", token: "t", target: "x", task: { steps: ["x".repeat(5000)] } }), /run hook limit/);
});

test("jobs need the key and a valid list of steps", async () => {
  assert.equal((await post("/jobs", { steps: ["ls"] })).status, 401);
  assert.equal((await post("/jobs", { steps: [] }, { "x-demo-key": "k3y" })).status, 400);
  assert.equal((await post("/jobs", { steps: Array(9).fill("ls") }, { "x-demo-key": "k3y" })).status, 400);
  assert.equal(vmCalls.length, 0);
});

test("a job launches one VM with the lease, ingress for streaming, and a duration cap", async () => {
  const { job, payload } = await startJob();
  const run = vmCalls.find((c) => c.name === "RunMicrovmCommand").input;
  assert.equal(job.microvmId, "microvm-1");
  assert.equal(payload.lease.target, `https://web.example/callbacks/${job.id}`);
  assert.equal(run.maximumDurationInSeconds, 360);
  assert.equal(run.idlePolicy.autoResumeEnabled, false);
  assert.match(run.ingressNetworkConnectors[0], /ALL_INGRESS$/);
  assert.match(run.egressNetworkConnectors[0], /INTERNET_EGRESS$/);
  assert.equal(run.clientToken.length, 64);
  assert.equal(items.get(job.id).state, "running");
  assert.ok(!("token" in items.get(job.id)), "only the token's hash is stored");
});

test("the VM heartbeats and completes the lease once; the function terminates it", async () => {
  const { job, token } = await startJob();
  const cb = (body, t = token) => post(`/callbacks/${job.id}`, body, { authorization: `Bearer ${t}` });
  assert.equal((await cb({ status: "heartbeat" }, "wrong")).status, 403);
  assert.equal((await cb({ status: "heartbeat", microvm_id: "microvm-1" })).status, 200);
  assert.equal(items.get(job.id).heartbeats, 1);
  assert.equal((await cb({ status: "success", microvm_id: "microvm-1", result: { passed: true } })).status, 200);
  assert.equal(items.get(job.id).state, "succeeded");
  assert.deepEqual(items.get(job.id).outcome.result, { passed: true });
  assert.deepEqual(vmCalls.filter((c) => c.name === "TerminateMicrovmCommand").map((c) => c.input), [{ microvmIdentifier: "microvm-1" }]);
  // a late heartbeat or a second completion finds the lease closed, so the VM's runtime stops
  assert.equal((await cb({ status: "heartbeat" })).status, 410);
  assert.equal((await post("/callbacks/nope", { status: "heartbeat" }, { authorization: "Bearer x" })).status, 404);
  const r = await fetch(`${base}/jobs/${job.id}`, { headers: { "x-demo-key": "k3y" } });
  const rec = await r.json();
  assert.equal(rec.state, "succeeded");
  assert.ok(!("tokenHash" in rec) && !("endpoint" in rec));
});

test("a typed failure is recorded as failed", async () => {
  const { job, token } = await startJob();
  const err = { error_type: "StepFailed", message: "step 1 exited 2" };
  await post(`/callbacks/${job.id}`, { status: "failure", error: err }, { authorization: `Bearer ${token}` });
  assert.equal(items.get(job.id).state, "failed");
  assert.deepEqual(items.get(job.id).outcome.error, err);
});

test("a job that stops heartbeating is marked lost on read and its VM terminated", async () => {
  const { job } = await startJob();
  items.get(job.id).created -= 100;
  const rec = await (await fetch(`${base}/jobs/${job.id}`, { headers: { "x-demo-key": "k3y" } })).json();
  assert.equal(rec.state, "lost");
  assert.equal(rec.outcome.error.error_type, "HeartbeatTimeout");
  assert.equal(vmCalls.filter((c) => c.name === "TerminateMicrovmCommand").length, 1);
});

test("the hourly launch budget refuses launches past the cap", async () => {
  for (let i = 0; i < 3; i++) await startJob();
  const r = await post("/jobs", { steps: ["ls"] }, { "x-demo-key": "k3y" });
  assert.equal(r.status, 429);
  assert.equal(vmCalls.filter((c) => c.name === "RunMicrovmCommand").length, 3);
});

test("a launch the quota refuses fails the job and answers 503", async () => {
  const real = deps.vms.send;
  deps.vms.send = async (cmd) => {
    if (cmd.constructor.name === "RunMicrovmCommand") throw Object.assign(new Error("memory"), { name: "ServiceQuotaExceededException" });
    return real(cmd);
  };
  try {
    const r = await post("/jobs", { steps: ["ls"] }, { "x-demo-key": "k3y" });
    assert.equal(r.status, 503);
    const job = [...items.values()].find((j) => j.steps);
    assert.equal(job.state, "failed");
    assert.equal(job.outcome.error.error_type, "LaunchFailed");
  } finally { deps.vms.send = real; }
});
