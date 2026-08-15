# AI code runner — an LLM (Amazon Bedrock) writes code, the microVM runs it,
# errors feed back for self-repair. The blast radius of model-written code is
# one disposable Firecracker VM.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y python3.12 python3.12-pip && dnf clean all
RUN python3.12 -m pip install --no-cache-dir boto3 numpy pandas matplotlib

WORKDIR /app
COPY microvm_hooks.py app.py /app/

EXPOSE 8080
ENTRYPOINT ["python3.12", "/app/app.py"]
