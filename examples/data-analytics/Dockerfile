# Isolated analytics engine — DuckDB over S3 parquet, per-user isolation for
# LLM-generated SQL. Bulk data rides the S3 side channel via the execution
# role; only queries and result sets cross the (bandwidth-capped) endpoint.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y python3.12 python3.12-pip && dnf clean all
RUN python3.12 -m pip install --no-cache-dir duckdb pyarrow boto3

WORKDIR /app
COPY microvm_hooks.py app.py /app/

EXPOSE 8080
ENTRYPOINT ["python3.12", "/app/app.py"]
