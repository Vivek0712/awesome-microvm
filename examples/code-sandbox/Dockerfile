# Code execution sandbox — runs untrusted / AI-generated Python inside a
# Firecracker microVM. Built on the Lambda-managed AL2023 base (snapshot-safe
# OpenSSL); everything installed here is baked into the snapshot every VM clones.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y python3.12 python3.12-pip && dnf clean all
RUN python3.12 -m pip install --no-cache-dir numpy pandas requests

WORKDIR /app
COPY microvm_hooks.py app.py /app/

EXPOSE 8080
ENTRYPOINT ["python3.12", "/app/app.py"]
