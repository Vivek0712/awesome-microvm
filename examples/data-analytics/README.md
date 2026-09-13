# Sandboxed data analytics

DuckDB with S3 access through the execution role. Loaded tables persist between queries. Bulk data rides S3 rather than the bandwidth-capped endpoint.

```console
mvm image build data-analytics examples/data-analytics
mvm run data-analytics --wait
mvm call <id> /load -X POST -d '{"table":"trips","s3_uri":"s3://bucket/trips.parquet"}'
mvm call <id> /query -X POST -d '{"sql":"SELECT COUNT(*) FROM trips"}'
```

Article: [Sandboxed data analytics with DuckDB on AWS Lambda MicroVMs](../../blog/05-data-analytics.md). Live transcript: [demo-data-analytics.png](../../blog/img/demo-data-analytics.png).
