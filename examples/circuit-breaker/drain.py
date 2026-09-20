"""On the alarm topic: terminate every microVM of the configured images.

`IMAGES_TO_DRAIN` is a comma-separated list of image names or ARNs, or `*` for every
image that has a live VM. Each image is drained with `Fleet(fm, image).drain()`, which
goes through the throttled TerminateMicrovm bucket, so a fleet of any size drains at the
account's rate and never trips the API quota while doing it.
"""

from __future__ import annotations

import logging
import os

from microvm import Fleet, FleetManager, PlaneConfig

IMAGES = os.environ.get("IMAGES_TO_DRAIN", "*")
log = logging.getLogger(__name__)


def handler(event, _context) -> dict:
    fm = FleetManager(PlaneConfig())
    if IMAGES.strip() == "*":
        images = sorted({vm.image_arn for vm in fm.list() if vm.state != "TERMINATED"})
    else:
        images = [s.strip() for s in IMAGES.split(",") if s.strip()]
    drained = {image.rsplit(":", 1)[-1]: Fleet(fm, image).drain() for image in images}
    log.warning("circuit breaker tripped (%s): drained %s", (event.get("Records") or [{}])[0].get("EventSource",
                "direct invoke"), drained)
    return {"drained": drained, "total": sum(drained.values())}
