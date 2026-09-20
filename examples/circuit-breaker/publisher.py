"""Every minute: the memory the account's microVMs hold, per image and in total.

Two custom metrics in the `microvm-ctl` namespace: `ActiveMicrovms` (PENDING, RUNNING,
SUSPENDING, SUSPENDED: what counts against the memory quota) and `RunningMemoryGiB` (the
memory of PENDING and RUNNING VMs: what is billed), each once per image (dimension
Image = image name) and once without dimensions as the account total the alarm watches.
Memory per VM is the image version's minimumMemoryInMiB, read once per (image, version)
and cached for the life of the container.
"""

from __future__ import annotations

import logging

from microvm import FleetManager, PlaneConfig
from microvm.client import lambda_client
from microvm.fleet import ACTIVE_STATES

NAMESPACE = "microvm-ctl"
BILLED = {"PENDING", "RUNNING"}
DEFAULT_MIB = 2048
log = logging.getLogger(__name__)
_memory: dict[tuple[str, str], int] = {}


def baseline_mib(api, image_arn: str, version: str) -> int:
    key = (image_arn, version)
    if key not in _memory:
        try:
            v = api.get_microvm_image_version(imageIdentifier=image_arn, imageVersion=version)
            _memory[key] = int(v["resources"][0]["minimumMemoryInMiB"])
        except Exception as e:  # a deleted version or a missing field: assume the 2 GB tier
            log.warning("no memory size for %s:%s (%s); assuming %d MiB", image_arn, version, e, DEFAULT_MIB)
            _memory[key] = DEFAULT_MIB
    return _memory[key]


def handler(_event, _context) -> dict:
    cfg = PlaneConfig()
    fm = FleetManager(cfg, quota_aware=False)          # reads only: no Service Quotas call needed
    per_image: dict[str, dict] = {}
    for vm in fm.list():
        if vm.state not in ACTIVE_STATES:
            continue
        row = per_image.setdefault(vm.image_arn.rsplit(":", 1)[-1], {"active": 0, "gib": 0.0})
        row["active"] += 1
        if vm.state in BILLED:
            row["gib"] += baseline_mib(fm.api, vm.image_arn, vm.image_version) / 1024
    total = {"active": sum(r["active"] for r in per_image.values()),
             "gib": sum(r["gib"] for r in per_image.values())}
    data = []
    for name, row in [*per_image.items(), (None, total)]:
        dims = [{"Name": "Image", "Value": name}] if name else []
        data.append({"MetricName": "ActiveMicrovms", "Dimensions": dims, "Value": row["active"], "Unit": "Count"})
        data.append({"MetricName": "RunningMemoryGiB", "Dimensions": dims, "Value": round(row["gib"], 3),
                     "Unit": "Gigabytes"})
    cw = lambda_client("cloudwatch", cfg.region, cfg.profile)
    for i in range(0, len(data), 1000):
        cw.put_metric_data(Namespace=NAMESPACE, MetricData=data[i:i + 1000])
    log.info("published %d datapoints: %s, total %s", len(data), per_image, total)
    return {"images": per_image, "total": total}
