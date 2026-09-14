# Sandboxed data analytics

DuckDB with S3 access through the execution role. Loaded tables persist between queries. Bulk data rides S3 rather than the bandwidth-capped endpoint.

```console
mvm image build data-analytics examples/data-analytics
mvm run data-analytics --wait
mvm call <id> /load -X POST -d '{"table":"trips","s3_uri":"s3://bucket/trips.parquet"}'
mvm call <id> /query -X POST -d '{"sql":"SELECT COUNT(*) FROM trips"}'
```

Series: [part 2, section 5 of Building on AWS Lambda MicroVMs](https://builder.aws.com/content/3JJ2oNWY9EsZzivMMx044cSlrFQ/seven-workloads-lambda-could-never-run-until-microvms). Full write-up: [Sandboxed data analytics with DuckDB on AWS Lambda MicroVMs](../../blog/deep-dives/05-data-analytics.md). Code: [github.com/Vivek0712/awesome-microvm/tree/main/examples/data-analytics](https://github.com/Vivek0712/awesome-microvm/tree/main/examples/data-analytics). Live transcript: [demo-data-analytics.png](../../blog/img/demo-data-analytics.png).
