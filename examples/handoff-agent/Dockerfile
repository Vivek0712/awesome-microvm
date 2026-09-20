# Handoff agent: the one image every orchestrator leases (Step Functions, a durable
# function, a queue poller, or a laptop). Nothing per-task lives in the snapshot: the
# lease and the task arrive in runHookPayload at launch and the hook runtime completes
# the lease itself through the matching AWS API or HTTP target.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y python3.12 python3.12-pip git tar gzip && dnf clean all
# al2023-minimal ships python3.12 only; give task steps a plain `python3` and `pip` as well
RUN ln -sf /usr/bin/python3.12 /usr/bin/python3 && ln -sf /usr/bin/pip3.12 /usr/bin/pip3 && ln -sf /usr/bin/pip3.12 /usr/bin/pip
# boto3 >= 1.43 carries SendTask* for Step Functions and SendDurableExecutionCallback* for durable functions
RUN python3.12 -m pip install --no-cache-dir "boto3>=1.43.0"

WORKDIR /app
COPY microvm_hooks.py app.py /app/

EXPOSE 8080
ENTRYPOINT ["python3.12", "/app/app.py"]
