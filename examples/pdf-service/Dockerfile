# HTML -> PDF renderer — untrusted markup rendered in a disposable VM.
# WeasyPrint (pure ARM64-friendly stack, no headless browser) keeps the
# snapshot small, which keeps launch/resume fast and storage cheap.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y python3.12 python3.12-pip pango cairo gdk-pixbuf2 && dnf clean all
RUN python3.12 -m pip install --no-cache-dir weasyprint

WORKDIR /app
COPY microvm_hooks.py app.py /app/

EXPOSE 8080
ENTRYPOINT ["python3.12", "/app/app.py"]
