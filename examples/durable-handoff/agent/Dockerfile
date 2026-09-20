# Security review agent, leased to a Lambda durable function one pull request at a time.
# The toolchain (git, bandit, boto3) is baked into the snapshot at /ready, so a clone
# is serving three seconds after RunMicrovm. Nothing per-PR lives in the image: the
# task and the durable callback id arrive in runHookPayload at launch.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y python3.12 python3.12-pip git tar gzip && dnf clean all
# boto3 >= 1.43 carries SendDurableExecutionCallback{Success,Failure,Heartbeat}
RUN python3.12 -m pip install --no-cache-dir "boto3>=1.43.0" bandit

WORKDIR /app
COPY microvm_hooks.py app.py /app/

EXPOSE 8080
ENTRYPOINT ["python3.12", "/app/app.py"]
