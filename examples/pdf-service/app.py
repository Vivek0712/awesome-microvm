"""HTML -> PDF rendering service.

POST /render {"html": "<h1>Invoice</h1>..."}  -> application/pdf bytes

Rendering user-supplied markup is code execution in a fancy hat (SSRF via
resources, parser exploits) — each render happens inside the Firecracker
boundary, and the idle policy suspends the VM between bursts so a rarely-used
internal service costs near nothing at rest.
"""

import base64
import time

from microvm_hooks import HookApp

app = HookApp()
STATS = {"renders": 0}


@app.on_ready
def ready(_ctx):
    global HTML
    from weasyprint import HTML  # heavyweight import baked warm into the snapshot
    return True


@app.on_validate
def validate(_ctx):
    HTML(string="<h1>warmup</h1>").write_pdf()  # prefetch the render path


@app.route("POST", "/render")
def render(body, _headers):
    html = body.get("html")
    if not html:
        return 400, {"error": "need 'html'"}
    started = time.time()
    pdf = HTML(string=html, base_url=None).write_pdf()
    STATS["renders"] += 1
    return 200, {
        "pdf_base64": base64.b64encode(pdf).decode(),
        "bytes": len(pdf),
        "render_ms": round((time.time() - started) * 1000, 1),
        "renders_this_vm": STATS["renders"],
    }


if __name__ == "__main__":
    app.serve(port=8080)
