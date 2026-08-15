# Sandboxed data analytics

DuckDB with S3 access via the execution role; loaded tables persist between queries. Bulk data rides S3, not the bandwidth-capped endpoint.

```console
mvm image build data-analytics examples/data-analytics
mvm run data-analytics --wait
mvm call <id> /load -X POST -d '{"table":"trips","s3_uri":"s3://bucket/trips.parquet"}'
mvm call <id> /query -X POST -d '{"sql":"SELECT COUNT(*) FROM trips"}'
```

Deep dive: [blog post](../../blog/05-data-analytics.md) · live transcript: [screenshot](../../benchmarks/results/demo-data-analytics.svg)
