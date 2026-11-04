"""Replay the forced interleaving against the PRE-FIX coordinator.

Stash the fix, run this, unstash.  Shows the stranded-switch failure the new
test guards against.
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import load_coordinator  # noqa: E402

mod, const = load_coordinator()
C = mod.BatteryOptimizerCoordinator

INSIDE = datetime(2026, 10, 6, 7, 45)
OUTSIDE = datetime(2026, 10, 6, 20, 0)

started = asyncio.Event()
release = asyncio.Event()
switch_calls = []

c = C.__new__(C)
c._manual_selling_active = False
c._manual_selling_served = None
# Read by the forced-sell-disabled guard that _evaluate_forced_sell() runs now.
# It is a live read of the config entry, so it is staged there and not here.
c._emaldo_control_enabled = True
c._entry = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
c._entry.data = {}
c._entry.options = {}
c._last_optimizer_attempt = INSIDE
c._forced_sell_tick_in_flight = False
c._sell_back_to_grid_saved = None
c._resolve_emaldo_entity = (
    lambda key, domain="sensor": f"{domain}.power_store_{key}")
c.resolve_emaldo_device = lambda: True


class _P:
    sell_slots = [30, 31, 32]
    sell_target_kwh = 5.0


plan = _P()
c._last_result = plan


async def fake_call(domain, service, *a, **k):
    if domain == "switch":
        switch_calls.append(service)
    if domain == "number" and service == "set_value":
        started.set()
        await release.wait()


c.hass = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
c.hass.services.async_call = fake_call


async def scenario():
    async def run_start():
        await c._manage_manual_selling(plan, INSIDE)

    async def run_tick():
        await started.wait()
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(
            mod.dt_util, "now", return_value=OUTSIDE
        ):
            await c._evaluate_forced_sell()

    a = asyncio.create_task(run_start())
    b = asyncio.create_task(run_tick())
    await started.wait()
    await b          # tick's stop completes while the start is parked
    release.set()
    await a


asyncio.run(scenario())

print("switch calls      :", switch_calls)
print("final switch state:", "ON  <-- STRANDED" if switch_calls[-1] == "turn_on" else "off")
print("_manual_selling_active:", c._manual_selling_active)
print("recoverable?      :",
      "no - every later _stop_manual_selling() early-returns on the flag"
      if not c._manual_selling_active and switch_calls[-1] == "turn_on"
      else "yes")
