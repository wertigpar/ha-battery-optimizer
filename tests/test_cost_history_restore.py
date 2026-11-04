"""Eager cost-history restore test (coordinator method, mocked hass).

Regression: after an HA restart the realized-cost sensors published 0.0
for up to 15 minutes because the persisted sidecar was only loaded lazily
on the first 15-minute capture tick.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from custom_components.battery_optimizer.coordinator import (
    BatteryOptimizerCoordinator,
)

RECORDS = [
    {
        "ts": "2026-08-26T00:15:00+03:00",
        "slot": 1,
        "buy": 0.1,
        "sell": 0.05,
        "import_kwh": 1.0,
        "export_kwh": 0.0,
        "bill": 0.1,
        "refund": 0.0,
        "net": 0.1,
    }
]


def test_eager_restore_loads_sidecar_once():
    coord = object.__new__(BatteryOptimizerCoordinator)
    coord._cost_history = None

    async def fake_executor(fn, *args):
        return RECORDS

    coord.hass = MagicMock()
    coord.hass.async_add_executor_job = fake_executor

    asyncio.run(coord.async_restore_cost_history())
    assert coord._cost_history == RECORDS

    # Idempotent: already loaded -> no second sidecar read.
    coord.hass.async_add_executor_job = MagicMock(
        side_effect=AssertionError("sidecar re-read after eager restore")
    )
    asyncio.run(coord.async_restore_cost_history())
    assert coord._cost_history == RECORDS