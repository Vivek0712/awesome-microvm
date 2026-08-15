# Multi-tenant AI agents — one microVM per tenant, identity injected at run
# time via runHookPayload. The image is tenant-agnostic; suspended tenants
# cost snapshot storage only, so 1,000 mostly-idle tenants stay affordable.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y python3.12 python3.12-pip && dnf clean all
RUN python3.12 -m pip install --no-cache-dir boto3

WORKDIR /app
COPY microvm_hooks.py app.py /app/

EXPOSE 8080
ENTRYPOINT ["python3.12", "/app/app.py"]
