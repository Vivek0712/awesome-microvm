"""Build the Python worker image with every lifecycle hook the durable SDK's operations use.

    python examples/durable-sdk-worker/build.py [image-name]

microvm-ctl injects microvm_hooks.py (the hook runtime) next to app.py, so the Dockerfile can copy it.
"""
import sys
from pathlib import Path

from microvm import ImageBuilder, PlaneConfig

NAME = sys.argv[1] if len(sys.argv) > 1 else "durable-sdk-worker-py"
HOOKS = {"port": 8080,
         "microvmHooks": {"run": "ENABLED", "runTimeoutInSeconds": 10, "resume": "ENABLED", "resumeTimeoutInSeconds": 10,
                          "suspend": "ENABLED", "suspendTimeoutInSeconds": 10,
                          "terminate": "ENABLED", "terminateTimeoutInSeconds": 10},
         "microvmImageHooks": {"ready": "ENABLED", "readyTimeoutInSeconds": 60}}

b = ImageBuilder(PlaneConfig())
built = b.build(NAME, str(Path(__file__).parent), memory_mib=1024, hooks=HOOKS, wait=True, log=print,
                description="microvm-ctl hook runtime (Python) serving the AWS durable execution SDK job contract")
print(b.arn(NAME), "version", built.version)
