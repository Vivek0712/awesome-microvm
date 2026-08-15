"""Sandboxed analytics: DuckDB with S3 access, one engine per user/agent.

POST /query {"sql": "SELECT ... FROM read_parquet('s3://bucket/x.parquet')"}
POST /load  {"table": "trips", "s3_uri": "s3://bucket/trips.parquet"}

The engine keeps loaded tables in VM memory/disk between queries — an
interactive session, not a stateless query API — and the whole working set
survives suspend/resume.
"""

import time

from microvm_hooks import HookApp

app = HookApp()
_db = None


@app.on_ready
def ready(_ctx):
    global _db
    import duckdb
    _db = duckdb.connect("/tmp/analytics.db")
    _db.execute("INSTALL httpfs; LOAD httpfs; INSTALL aws; LOAD aws;")
    return True


@app.on_run
def on_run(_ctx):
    # Credentials come from the execution role at run time — never the snapshot.
    # Never let credential setup fail the /run hook: a non-200 terminates the VM;
    # a VM without S3 access can still serve local queries.
    try:
        _db.execute("CREATE OR REPLACE SECRET aws (TYPE s3, PROVIDER credential_chain);")
    except Exception as e:
        print(f"s3 secret setup skipped: {e}", flush=True)


@app.on_resume
def on_resume(ctx):
    on_run(ctx)  # role credentials rotate; refresh after resume


@app.route("POST", "/load")
def load(body, _headers):
    table, uri = body.get("table"), body.get("s3_uri")
    if not table or not uri:
        return 400, {"error": "need 'table' and 's3_uri'"}
    started = time.time()
    _db.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM read_parquet(?)", [uri])
    rows = _db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return 200, {"table": table, "rows": rows, "load_ms": round((time.time() - started) * 1000)}


@app.route("POST", "/query")
def query(body, _headers):
    sql = body.get("sql")
    if not sql:
        return 400, {"error": "need 'sql'"}
    started = time.time()
    try:
        cur = _db.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(1000)
    except Exception as e:
        return 400, {"error": str(e)[:2000]}
    return 200, {
        "columns": cols,
        "rows": [[str(v) for v in r] for r in rows],
        "row_count": len(rows),
        "query_ms": round((time.time() - started) * 1000, 1),
    }


@app.route("GET", "/tables")
def tables(_body, _headers):
    return 200, {"tables": [r[0] for r in _db.execute("SHOW TABLES").fetchall()]}


if __name__ == "__main__":
    app.serve(port=8080)
