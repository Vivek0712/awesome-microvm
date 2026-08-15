# Stateful notebook kernel — variables, dataframes, and imports live in VM
# memory across requests, survive suspend/resume byte-for-byte, and cost only
# snapshot storage while the user is at lunch.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y python3.12 python3.12-pip && dnf clean all
RUN python3.12 -m pip install --no-cache-dir numpy pandas matplotlib

WORKDIR /app
COPY microvm_hooks.py app.py /app/

EXPOSE 8080
ENTRYPOINT ["python3.12", "/app/app.py"]
