# Agent evaluation worker — one pristine, identical environment per eval run.
# Every VM is a byte-for-byte clone of the same snapshot: no contamination
# between runs, no flaky shared state, horizontal fan-out limited only by quota.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y python3.12 python3.12-pip git && dnf clean all
RUN python3.12 -m pip install --no-cache-dir pytest requests

WORKDIR /app
COPY microvm_hooks.py app.py /app/

EXPOSE 8080
ENTRYPOINT ["python3.12", "/app/app.py"]
