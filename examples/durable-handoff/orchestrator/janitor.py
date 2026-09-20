"""Scheduled janitor: terminate any agent VM older than the job budget plus slack.

The orchestrator terminates the VM it leased in every branch it can see. This covers
the branches it cannot: an operator calling StopDurableExecution (the invocation is
killed at the next checkpoint and no except block runs), a lost checkpoint, or a
launch whose result never made it back. maximumDurationInSeconds on the VM is the
last line; this runs every five minutes so the bill for a stray VM stays at minutes.
"""

from __future__ import annotations

import os

from microvm import Fleet, FleetManager, PlaneConfig

IMAGE = os.environ["MVM_IMAGE"]
MAX_AGE_S = int(os.environ.get("JOB_BUDGET_S", "900")) + 300


def handler(_event, _context) -> dict:
    fleet = Fleet(FleetManager(PlaneConfig()), IMAGE)
    reaped = fleet.reap(MAX_AGE_S)
    members = fleet.members()
    return {"reaped": reaped, "active": [m.microvm_id for m in members]}
