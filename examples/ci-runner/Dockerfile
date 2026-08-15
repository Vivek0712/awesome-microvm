# Ephemeral CI runner — clone, test, report, terminate (Pattern C: no suspend).
# Toolchain and warm pip cache are baked into the snapshot, so every job starts
# on a pre-warmed runner in seconds instead of provisioning one for minutes.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y python3.12 python3.12-pip git tar gzip && dnf clean all
RUN python3.12 -m pip install --no-cache-dir pytest coverage ruff

WORKDIR /app
COPY microvm_hooks.py app.py /app/

EXPOSE 8080
ENTRYPOINT ["python3.12", "/app/app.py"]
