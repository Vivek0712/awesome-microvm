---
title: "Sandboxed data analytics with DuckDB on AWS Lambda MicroVMs"
description: "One DuckDB engine per user or agent inside a Firecracker VM: loaded tables survive suspend, bulk data rides S3 instead of the bandwidth-capped endpoint, and the blast radius of a bad LLM-written query is exactly one VM."
series: "Building on AWS Lambda MicroVMs"
part: 6
tags: ["lambda", "data", "duckdb", "python", "ai"]
cover: "img/cover-05.png"
---

An LLM that writes SQL is an untrusted user with a keyboard. DuckDB will happily COPY to any path, read any file the process can see, and load extensions. Sanitizing the SQL does not contain that; the boundary around the process does. At the same time, analytics sessions are stateful. An analyst loads a parquet file once and then asks it forty questions, and re-scanning S3 for every question is the tax you pay for statelessness. We wanted hard isolation per user and a warm engine that keeps tables loaded between queries, without paying for a fleet of always-on database containers. On Lambda MicroVMs, our measured burst-analyst session shape (30 minutes active, 8 hours suspended) cost $0.0669 against $1.0719 always-on, a 93.8% saving.

This is part 6 of the series Building on AWS Lambda MicroVMs. AWS's launch material for the service lists data analytics, meaning notebooks and user- or LLM-supplied scripts with large working sets, as a core pattern, with ClickHouse's chDB as a launch partner for this shape. We built the DuckDB version.

## Why a microVM and not a container or a Lambda function

A Lambda function fails the statefulness test. Every invocation is a fresh process, so the table you loaded for query one is gone by query two, and each question re-pays the full S3 scan. A shared warehouse, or one big DuckDB service, fails the isolation test. LLM-generated SQL from tenant A runs in the same process that holds tenant B's data, and DuckDB's file and extension access makes read-only SQL a fiction. A per-user Fargate container gets both properties but bills every second the analyst is thinking, and analysts mostly think.

A microVM is a per-user process boundary that sleeps. Each engine is a Firecracker VM with its own kernel, its own disk, its own IAM-scoped credentials, and its own dedicated HTTPS endpoint. Loaded tables live in VM memory and on VM disk between queries. The whole working set survives suspend and resume (we verified the same process, PID 1 before and after, files intact), and a parked engine bills as snapshot storage rather than compute. A hostile query can trash its own VM, and `mvm terminate` is the cleanup.

## Architecture

![Data analytics architecture: SQL and small result sets cross the endpoint, bulk parquet moves between DuckDB and S3 over the execution role](img/arch-05-data-analytics.png)

One rule makes the whole design work: bulk data never crosses the endpoint. The per-VM endpoint is bandwidth-capped by VM size (1 MB/s on a 0.5 GB VM up to 16 MB/s on an 8 GB VM), which would make it a miserable pipe for a parquet file. So datasets ride the S3 side channel. DuckDB's httpfs extension reads s3:// URIs directly using the VM's execution-role credentials, and only two things cross the capped endpoint: the SQL going in and the result set coming out, capped at 1,000 rows. Both are small by construction.

## Build it

The Dockerfile has three meaningful lines:

```dockerfile
FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y python3.12 python3.12-pip && dnf clean all
RUN python3.12 -m pip install --no-cache-dir duckdb pyarrow boto3

WORKDIR /app
COPY microvm_hooks.py app.py /app/

EXPOSE 8080
ENTRYPOINT ["python3.12", "/app/app.py"]
```

The engineering is in the hooks, because each one maps to a snapshot-semantics rule.

/ready bakes the engine into the snapshot. The service only snapshots after /ready returns 200, so we open the database and install the S3 extensions there. Every clone wakes with httpfs and aws already loaded, cost paid once at build time:

```python
@app.on_ready
def ready(_ctx):
    global _db
    import duckdb
    _db = duckdb.connect("/tmp/analytics.db")
    _db.execute("INSTALL httpfs; LOAD httpfs; INSTALL aws; LOAD aws;")
    return True
```

/run creates credentials, and deliberately cannot fail. Credentials must not go in at build time, because the snapshot is cloned into every VM and a baked-in credential is a credential shared by every tenant. Instead each VM builds its S3 secret from its own execution role when it starts. The swallow-everything try block is there because a non-200 from /run terminates the VM, and an engine without S3 access is degraded rather than dead; it can still serve local queries:

```python
@app.on_run
def on_run(_ctx):
    # Credentials come from the execution role at run time, never the snapshot.
    # Never let credential setup fail the /run hook: a non-200 terminates the VM;
    # a VM without S3 access can still serve local queries.
    try:
        _db.execute("CREATE OR REPLACE SECRET aws (TYPE s3, PROVIDER credential_chain);")
    except Exception as e:
        print(f"s3 secret setup skipped: {e}", flush=True)


@app.on_resume
def on_resume(ctx):
    on_run(ctx)  # role credentials rotate; refresh after resume
```

/resume re-runs /run because a suspended VM's snapshot contains the credentials it had when it went to sleep, and role credentials rotate. An engine that sleeps through a rotation would wake with dead credentials and start failing S3 reads for no visible reason. Refreshing the secret on resume closes that hole.

The API surface is two routes. /load pulls a parquet file from S3 into a named table, the one moment bulk data moves, and it moves over the side channel:

```python
@app.route("POST", "/load")
def load(body, _headers):
    table, uri = body.get("table"), body.get("s3_uri")
    ...
    _db.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM read_parquet(?)", [uri])
```

/query runs whatever SQL arrives, which is the point: the VM is the sandbox, so the app does not pretend to be one. It catches any DuckDB error as a 400 with the message truncated to 2 KB and returns at most 1,000 rows with a measured query_ms. Errors are data for the LLM's repair loop.

```console
$ mvm image build data-analytics examples/data-analytics
$ mvm run data-analytics --wait
```

The image built in 133.7 s with a 680 MB memory snapshot, the largest of our eight images since DuckDB plus pyarrow is not small, and 26 MB of disk.

## Run it

The live transcript against the real service:

![Data analytics live demo: a query that fails cleanly on a missing module, then a one-million-row aggregation in 1.2 seconds](img/demo-data-analytics.png)

`mvm run data-analytics --wait` had the VM RUNNING and serving in 6.4 s in this capture, slower than our fleet-wide p50 of 3.54 s (p95 4.49 s). Launches vary, and this one drew a long straw.

The first /query in the transcript fails, and the failure is the useful part. The SQL touched a code path needing pytz, and DuckDB returned Invalid Input Error: Required module 'pytz' failed to import. The engine surfaced it as a 400 JSON body and kept serving, which is exactly the behavior you want when the SQL author is a language model that will read the error and try again. It is also the reality of a minimal image: our Dockerfile installs duckdb, pyarrow, and boto3 and nothing else, so timezone-flavored Python UDF paths are out until you add the package.

The second query is the real work: generate and aggregate one million rows with a GROUP BY into five buckets, counting and averaging. It returned five rows (bucket, n, mean, with counts of 142,857 to 142,858 and means around 499,999) with query_ms at 1,189.7 measured inside the VM. That is a million-row aggregation in about 1.2 s on a 2 GB / 1 vCPU microVM, with the result set crossing the endpoint as a few hundred bytes of JSON.

## What it costs

us-east-1 rates: $0.0000276944 per vCPU-second, $0.0000036667 per GB-second, suspended snapshot storage $0.08 per GB-month, snapshot write $0.0038 per GB, read $0.00155 per GB. Worked examples on a 2 GB / 1 vCPU VM:

| Session shape | MicroVM engine | Always-on engine | Saved |
|---|---|---|---|
| 30 min active + 8 h suspended | $0.0669 | $1.0719 | 93.8% |
| 2 h active + 22 h suspended | $0.2602 | $3.0264 | 91.4% |
| 24/7 always-on 2 GB | n/a | about $3.03 per day | the shape where microVMs lose |

The 30 minute shape is the analyst: load a table, fire a burst of questions, leave the tab open. An always-on per-user engine bills the tab; a microVM bills the burst. Parked, the engine costs snapshot storage, and our 680 MB snapshot at $0.08 per GB-month is about five and a half cents a month. Two caveats keep the math honest. A suspend and resume cycle costs about $0.0033 in snapshot write plus read on a 0.61 GB snapshot, so a thrashing idle policy pays a toll per nap. And one-shot queries should terminate rather than suspend; an 8 second one-shot ran us $0.0003.

## The gotchas

- The bandwidth cap is a design constraint. 1 MB/s (0.5 GB VM) to 16 MB/s (8 GB VM) on the endpoint. The moment someone POSTs the CSV or selects a big table back through the endpoint, queries crawl. Keep the discipline: data via S3, the endpoint for SQL and small results, and cap result rows in the app as /query's fetchmany(1000) does.
- Snapshots clone credentials, and credentials rotate. Never create the S3 secret at build time (shared by every tenant's clone). Create it in /run from the execution role, and re-create it in /resume because role credentials rotate while the VM sleeps.
- A failing hook is a dead VM. Any non-200 from /run terminates the machine. Wrap best-effort setup, like our credential bootstrap, so a transient IAM hiccup degrades the engine instead of killing it.
- Idle detection watches endpoint traffic, and suspendedDurationSeconds is a self-destruct timer. An analyst staring at results is idle and gets suspended, which is where the 93.8% comes from, but when the suspended TTL elapses the VM auto-terminates, loaded tables and all, inside a hard 8 hour total lifetime. Size the TTL to your users' longest absence and have an export story.

## Take it further

- Swap the engine. The hook pattern is engine-agnostic. ClickHouse's chDB drops into the same /ready-bake, /run-credentials skeleton.
- Close the LLM loop in-VM. Call Bedrock through the same execution role, as the AI code runner in part 3 does, so text-to-SQL, execution, and error-driven retry all happen inside the sandbox.
- Write big results to S3. For outputs over a few MB, COPY (...) TO 's3://...' over the side channel and return the URI. The same rule that governs ingress governs egress.

Code and transcripts are in [awesome-microvm](https://github.com/Vivek0712/awesome-microvm) under examples/data-analytics. The plane is [microvm-ctl](https://github.com/Vivek0712/microvm-ctl). This is part 6 of Building on AWS Lambda MicroVMs; part 7 moves from sessions that sleep to jobs that die: an ephemeral CI runner.
