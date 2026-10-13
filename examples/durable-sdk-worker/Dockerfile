FROM public.ecr.aws/lambda/microvms:al2023-minimal
RUN dnf install -y python3.12 python3.12-pip && dnf clean all
# botocore 1.43+ carries the durable callback APIs and the lambda-microvms model (for SuspendMicrovm)
RUN python3.12 -m pip install --no-cache-dir "boto3>=1.43.60"
WORKDIR /app
COPY microvm_hooks.py app.py /app/
EXPOSE 8080
ENTRYPOINT ["python3.12", "/app/app.py"]
