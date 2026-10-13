// A Lambda Web Function that hands jobs to Lambda MicroVMs.
//
// The Web Function is the front door and the orchestrator. For each job it launches a MicroVM with a
// microvm-ctl HTTP lease in runHookPayload whose target is this Web Function's own endpoint. The VM runs
// the job, heartbeats and completes the lease by POSTing to /callbacks/<id> with the lease token, and
// this function records the outcome in DynamoDB and terminates the VM. A caller can wait for the job on
// one streamed response (the VM's live log, proxied from its /events), or take the 202 and poll.
//
// Routes:
//   GET  /                      a small page to submit steps and watch them run
//   POST /jobs                  {steps: [...]}; 202 {id, microvmId}, or ?stream=1 for an SSE stream to the end
//   GET  /jobs/:id              the job record
//   GET  /jobs/:id/events       SSE: the VM's log while it runs, then the result
//   POST /callbacks/:id         the VM's lease completer (Bearer <lease token>); heartbeat, success, failure
//   GET  /healthz
import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import http from "node:http";
import {
  CreateMicrovmAuthTokenCommand, LambdaMicrovmsClient, RunMicrovmCommand, TerminateMicrovmCommand,
} from "@aws-sdk/client-lambda-microvms";
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { DynamoDBDocumentClient, GetCommand, PutCommand, UpdateCommand } from "@aws-sdk/lib-dynamodb";
import { PAGE } from "./page.mjs";

const env = process.env;
const REGION = env.AWS_REGION || "us-east-1";
const IMAGE = env.MICROVM_IMAGE_ARN;
const TABLE = env.JOBS_TABLE;
const KEY = env.DEMO_KEY || "";
const BUDGET_S = Number(env.JOB_BUDGET_S || 300);          // the VM's idle cap; the lease must end by then
const SLACK_S = 60;                                          // maximumDurationInSeconds = budget + slack
const HEARTBEAT_S = Number(env.HEARTBEAT_S || 10);
const LAUNCHES_PER_HOUR = Number(env.LAUNCHES_PER_HOUR || 30);
const MAX_STEPS = 8, MAX_STEP_CHARS = 300, PAYLOAD_LIMIT = 4096;
const connector = (n) => `arn:aws:lambda:${REGION}:aws:network-connector:aws-network-connector:${n}`;

export const deps = {
  vms: new LambdaMicrovmsClient({ region: REGION }),
  db: DynamoDBDocumentClient.from(new DynamoDBClient({ region: REGION }), {
    marshallOptions: { removeUndefinedValues: true },
  }),
  fetch: (...a) => fetch(...a),
};

const sha256 = (s) => createHash("sha256").update(s).digest("hex");
const now = () => Math.floor(Date.now() / 1000);
const log = (msg, fields = {}) => console.log(JSON.stringify({ msg, ...fields }));

class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

function json(res, status, body) {
  res.writeHead(status, { "content-type": "application/json", "cache-control": "no-store" });
  res.end(JSON.stringify(body));
}

async function readJson(req, limit = 512 * 1024) {
  let size = 0;
  const chunks = [];
  for await (const c of req) {
    size += c.length;
    if (size > limit) throw new HttpError(413, "body too large");
    chunks.push(c);
  }
  if (!chunks.length) return {};
  try { return JSON.parse(Buffer.concat(chunks).toString("utf8")); }
  catch { throw new HttpError(400, "body is not JSON"); }
}

function requireKey(req) {
  const got = String(req.headers["x-demo-key"] || "");
  if (!KEY || got.length !== KEY.length || !timingSafeEqual(Buffer.from(got), Buffer.from(KEY))) {
    throw new HttpError(401, "missing or wrong x-demo-key");
  }
}

export function validateSteps(body) {
  const steps = body?.steps;
  if (!Array.isArray(steps) || steps.length === 0 || steps.length > MAX_STEPS) {
    throw new HttpError(400, `steps must be a list of 1 to ${MAX_STEPS} shell commands`);
  }
  for (const s of steps) {
    if (typeof s !== "string" || !s.trim() || s.length > MAX_STEP_CHARS) {
      throw new HttpError(400, `each step must be a non-empty string of at most ${MAX_STEP_CHARS} characters`);
    }
  }
  return { steps, parallel: body.parallel === true };
}

// The lease payload the VM's microvm-ctl runtime reads from the run hook (microvm.lease.encode_payload).
export function leasePayload({ id, token, target, task }) {
  const raw = JSON.stringify({
    lease: { kind: "http", token, region: REGION, target, heartbeat_s: HEARTBEAT_S, id },
    task,
  });
  if ([...raw].length > PAYLOAD_LIMIT) throw new HttpError(400, `job is over the ${PAYLOAD_LIMIT} character run hook limit`);
  return raw;
}

async function spendLaunch() {
  const hour = new Date().toISOString().slice(0, 13);
  try {
    await deps.db.send(new UpdateCommand({
      TableName: TABLE, Key: { id: `budget#${hour}` },
      UpdateExpression: "ADD n :one SET expires = :exp",
      ConditionExpression: "attribute_not_exists(n) OR n < :cap",
      ExpressionAttributeValues: { ":one": 1, ":cap": LAUNCHES_PER_HOUR, ":exp": now() + 7200 },
    }));
  } catch (e) {
    if (e.name === "ConditionalCheckFailedException") throw new HttpError(429, "launch budget for this hour is spent");
    throw e;
  }
}

function selfUrl(req) {
  return env.PUBLIC_URL || `https://${req.headers.host}`;
}

async function startJob(req, body) {
  if (!IMAGE || !TABLE) throw new HttpError(500, "MICROVM_IMAGE_ARN and JOBS_TABLE must be set");
  const task = validateSteps(body);
  const id = randomBytes(9).toString("base64url");
  const token = randomBytes(32).toString("hex");
  const payload = leasePayload({ id, token, target: `${selfUrl(req)}/callbacks/${id}`, task });
  await spendLaunch();
  const created = now();
  await deps.db.send(new PutCommand({
    TableName: TABLE,
    Item: { id, state: "launching", tokenHash: sha256(token), steps: task.steps, created, heartbeats: 0,
            expires: created + 7 * 86400 },
  }));
  let vm;
  const t0 = Date.now();
  try {
    vm = await deps.vms.send(new RunMicrovmCommand({
      imageIdentifier: IMAGE,
      runHookPayload: payload,
      clientToken: sha256(`http:${token}:${id}`).slice(0, 64),   // a retried launch returns the same VM
      maximumDurationInSeconds: BUDGET_S + SLACK_S,
      idlePolicy: { maxIdleDurationSeconds: BUDGET_S, suspendedDurationSeconds: 60, autoResumeEnabled: false },
      ingressNetworkConnectors: [connector("ALL_INGRESS")],      // so this function can stream the VM's /events
      egressNetworkConnectors: [connector("INTERNET_EGRESS")],   // so the VM can call /callbacks back
    }));
  } catch (e) {
    await finish(id, "failed", { error: { error_type: "LaunchFailed", message: `${e.name}: ${e.message}` } });
    throw new HttpError(e.name === "ServiceQuotaExceededException" || e.name === "ThrottlingException" ? 503 : 502,
                        `RunMicrovm failed: ${e.name}`);
  }
  const launchMs = Date.now() - t0;
  // the callback can land before this update on a fast job, so only move launching -> running
  await deps.db.send(new UpdateCommand({
    TableName: TABLE, Key: { id },
    UpdateExpression: "SET microvmId = :vm, endpoint = :ep, launchMs = :ms",
    ExpressionAttributeValues: { ":vm": vm.microvmId, ":ep": vm.endpoint, ":ms": launchMs },
  }));
  await deps.db.send(new UpdateCommand({
    TableName: TABLE, Key: { id }, UpdateExpression: "SET #s = :r",
    ConditionExpression: "#s = :l", ExpressionAttributeNames: { "#s": "state" },
    ExpressionAttributeValues: { ":r": "running", ":l": "launching" },
  })).catch((e) => { if (e.name !== "ConditionalCheckFailedException") throw e; });
  log("launched", { id, microvmId: vm.microvmId, launchMs });
  return { id, microvmId: vm.microvmId, endpoint: vm.endpoint, launchMs };
}

async function getJob(id) {
  const r = await deps.db.send(new GetCommand({ TableName: TABLE, Key: { id } }));
  if (!r.Item || id.startsWith("budget#")) return null;
  return r.Item;
}

const DONE = new Set(["succeeded", "failed", "lost"]);

// Move a job to a final state once; the first writer wins, and only it terminates the VM.
async function finish(id, state, payload) {
  try {
    const r = await deps.db.send(new UpdateCommand({
      TableName: TABLE, Key: { id },
      UpdateExpression: "SET #s = :s, finished = :f, outcome = :o",
      ConditionExpression: "attribute_exists(id) AND NOT #s IN (:a, :b, :c)",
      ExpressionAttributeNames: { "#s": "state" },
      ExpressionAttributeValues: { ":s": state, ":f": now(), ":o": payload, ":a": "succeeded", ":b": "failed", ":c": "lost" },
      ReturnValues: "ALL_NEW",
    }));
    const vmId = r.Attributes?.microvmId || payload?.microvm_id;
    if (vmId) {
      await deps.vms.send(new TerminateMicrovmCommand({ microvmIdentifier: vmId }))
        .catch((e) => log("terminate failed", { id, vmId, error: e.name }));
    }
    return true;
  } catch (e) {
    if (e.name === "ConditionalCheckFailedException") return false;
    throw e;
  }
}

function publicJob(j) {
  const { tokenHash, endpoint, expires, ...rest } = j;
  return rest;
}

async function callback(req, res, id) {
  const job = await getJob(id);
  if (!job) return json(res, 404, { error: "no such lease" });              // the VM stops: token closed
  const auth = String(req.headers.authorization || "");
  const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  const want = Buffer.from(job.tokenHash, "hex"), got = Buffer.from(sha256(token), "hex");
  if (!timingSafeEqual(want, got)) return json(res, 403, { error: "wrong lease token" });
  if (DONE.has(job.state)) return json(res, 410, { error: `lease is ${job.state}` });
  const body = await readJson(req);
  if (body.status === "heartbeat") {
    await deps.db.send(new UpdateCommand({
      TableName: TABLE, Key: { id },
      UpdateExpression: "ADD heartbeats :one SET lastHeartbeat = :t",
      ExpressionAttributeValues: { ":one": 1, ":t": now() },
    }));
    return json(res, 200, { ok: true });
  }
  if (body.status !== "success" && body.status !== "failure") return json(res, 400, { error: "unknown status" });
  const { status, ...payload } = body;
  const first = await finish(id, status === "success" ? "succeeded" : "failed", payload);
  log("lease completed", { id, status, first });
  return json(res, first ? 200 : 410, { ok: first });
}

// A job whose VM stopped heartbeating is lost: mark it and terminate the VM. Checked on read, so a job
// nobody looks at is still bounded by maximumDurationInSeconds.
async function reapIfStale(job) {
  if (DONE.has(job.state)) return job;
  const last = job.lastHeartbeat || job.created;
  if (now() - last <= 3 * HEARTBEAT_S + 30) return job;
  await finish(job.id, "lost", { error: { error_type: "HeartbeatTimeout", message: `no heartbeat for ${now() - last} s` } });
  return getJob(job.id);
}

async function vmEventsStream(job, signal) {
  const tok = await deps.vms.send(new CreateMicrovmAuthTokenCommand({
    microvmIdentifier: job.microvmId, expirationInMinutes: 15, allowedPorts: [{ port: 8080 }],
  }));
  const headers = { ...tok.authToken, accept: "text/event-stream" };
  const url = `https://${job.endpoint.replace(/^https?:\/\//, "")}/events`;
  for (let attempt = 0; ; attempt++) {
    const r = await deps.fetch(url, { headers, signal });
    if (r.ok) return r.body;
    if (attempt >= 10) throw new Error(`VM /events answered ${r.status}`);
    await new Promise((ok) => setTimeout(ok, 500));
  }
}

// One SSE response from launch to result: the VM's log while it runs, then the job record.
async function streamJob(req, res, id, started) {
  res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-store", "x-accel-buffering": "no" });
  const send = (event, data) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  if (started) send("launched", { id: started.id, microvmId: started.microvmId, launchMs: started.launchMs });
  const ac = new AbortController();
  req.on("close", () => ac.abort());
  let job = await getJob(id);
  const pump = (async () => {
    if (!job?.endpoint || DONE.has(job.state)) return;
    try {
      const body = await vmEventsStream(job, ac.signal);
      const dec = new TextDecoder();
      let buf = "";
      for await (const chunk of body) {
        buf += dec.decode(chunk, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, i); buf = buf.slice(i + 2);
          const ev = /^event: (.*)$/m.exec(frame)?.[1] || "log";
          const data = /^data: (.*)$/m.exec(frame)?.[1];
          if (data) res.write(`event: ${ev === "snapshot" ? "snapshot" : "log"}\ndata: ${data}\n\n`);
        }
      }
    } catch (e) {
      if (!ac.signal.aborted) send("note", { message: `VM log stream ended: ${e.message}` });
    }
  })();
  while (!ac.signal.aborted) {
    job = await reapIfStale(await getJob(id));
    if (!job || DONE.has(job.state)) break;
    await new Promise((ok) => setTimeout(ok, 1000));
  }
  ac.abort();
  await pump.catch(() => {});
  if (job) send("result", publicJob(job));
  res.end();
}

async function route(req, res) {
  const url = new URL(req.url, "http://x");
  const path = url.pathname;
  let m;
  if (req.method === "GET" && path === "/") {
    res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    return res.end(PAGE);
  }
  if (req.method === "GET" && path === "/healthz") return json(res, 200, { ok: true });
  if ((m = /^\/callbacks\/([\w-]{1,32})$/.exec(path)) && req.method === "POST") return callback(req, res, m[1]);
  if (req.method === "POST" && path === "/jobs") {
    requireKey(req);
    const started = await startJob(req, await readJson(req));
    if (url.searchParams.get("stream") === "1") return streamJob(req, res, started.id, started);
    return json(res, 202, { id: started.id, microvmId: started.microvmId, launchMs: started.launchMs });
  }
  if ((m = /^\/jobs\/([\w-]{1,32})(\/events)?$/.exec(path)) && req.method === "GET") {
    requireKey(req);
    const job = await getJob(m[1]);
    if (!job) throw new HttpError(404, "no such job");
    if (m[2]) return streamJob(req, res, m[1], null);
    return json(res, 200, publicJob(await reapIfStale(job)));
  }
  throw new HttpError(404, "not found");
}

export const server = http.createServer((req, res) => {
  route(req, res).catch((e) => {
    const status = e instanceof HttpError ? e.status : 500;
    if (status === 500) log("error", { error: e.name, message: e.message, stack: e.stack });
    if (!res.headersSent) json(res, status, { error: status === 500 ? "internal error" : e.message });
    else res.end();
  });
});

if (env.NO_LISTEN !== "1") {
  const [host, port] = (env.AWS_LAMBDA_HTTP_ENDPOINT || "127.0.0.1:3000").split(":");
  server.listen(Number(port), host, () => log("listening", { host, port }));
  process.on("SIGTERM", () => process.exit(0));
}
