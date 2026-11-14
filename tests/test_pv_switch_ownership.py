"""Third-party PV switch ownership: the optimizer may not claim a switch it never took.

With **PV Sell Strategy** off — the default — every optimizer run reached
`_apply_pv_strategy`, which used to call `_ensure_pv_switch_matches_plan(True)`
unconditionally.  That read the real switch state, logged
`PV switch mismatch: actual=off, desired=on — correcting` and issued
`switch.turn_on` on `switch.<device>_third_party_pv`.  On a unit without
third-party PV that is a write to a switch the user owns, and it changes the
Emaldo's control model (top-off charge pulses, fan starts) for a system that has
no third-party PV to sell.

The restore is now gated on `_pv_switch_owned`: only a state this integration
wrote may be handed back.  Driven against a `SimpleNamespace` fake `self` with
the real methods bound, so the service-call recorder sees exactly what HA would
have received.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from . import load_coordinator

coord, const = load_coordinator()
Coord = coord.BatteryOptimizerCoordinator

PV_SWITCH = "switch.power_store_third_party_pv"


class _FakeStates:
    def __init__(self, state):
        self._state = state

    def get(self, entity_id):
        return self._state


class _StateObj:
    def __init__(self, state):
        self.state = state


class _FakeServices:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, **kwargs):
        self.calls.append((domain, service, data))


class _FakeSelf:
    """Minimal ``self`` for the PV-switch apply path."""

    _apply_pv_strategy = Coord._apply_pv_strategy
    _ensure_pv_switch_matches_plan = Coord._ensure_pv_switch_matches_plan
    _set_pv_switch = Coord._set_pv_switch

    def __init__(self, switch_state, strategy_enabled, owned, control_enabled=True):
        self.config = {}
        self.services = _FakeServices()
        self.hass = SimpleNamespace(
            services=self.services,
            states=_FakeStates(_StateObj(switch_state) if switch_state else None),
        )
        self._unsub_pv_transitions = []
        self._emaldo_control_enabled = control_enabled
        self._pv_strategy_enabled = strategy_enabled
        self._pv_switch_owned = owned
        self._pv_switch_state = None
        self._pv_cut_latch = True  # must be cleared regardless of the write
        self._resolve_emaldo_entity = lambda key, domain="sensor": PV_SWITCH

    async def apply(self):
        await self._apply_pv_strategy(None)  # result unused on the disabled path
        return self.services.calls


class StrategyOffTests(unittest.TestCase):
    def test_a_switch_never_written_is_left_alone(self):
        """No write, no 'mismatch — correcting' churn: the switch is the user's."""
        fake = _FakeSelf(switch_state="off", strategy_enabled=False, owned=None)
        self.assertEqual(asyncio.run(fake.apply()), [])
        self.assertFalse(fake._pv_cut_latch)
        self.assertIsNone(fake._pv_switch_owned)

    def test_a_switch_we_cut_off_is_handed_back(self):
        """The strategy had PV off; disabling the strategy gives it back."""
        fake = _FakeSelf(switch_state="off", strategy_enabled=False, owned=False)
        calls = asyncio.run(fake.apply())
        self.assertEqual(calls, [("switch", "turn_on", {"entity_id": PV_SWITCH})])
        # Ownership is released: the switch is the user's again from now on.
        self.assertIsNone(fake._pv_switch_owned)

    def test_a_switch_we_left_on_is_not_written_again(self):
        fake = _FakeSelf(switch_state="on", strategy_enabled=False, owned=True)
        self.assertEqual(asyncio.run(fake.apply()), [])
        self.assertIsNone(fake._pv_switch_owned)

    def test_disabled_emaldo_control_touches_nothing(self):
        """Even a switch we own stays untouched while Emaldo control is off."""
        fake = _FakeSelf(
            switch_state="off",
            strategy_enabled=False,
            owned=False,
            control_enabled=False,
        )
        self.assertEqual(asyncio.run(fake.apply()), [])
        self.assertTrue(fake._pv_cut_latch)  # the early return keeps state as-is


class StrategyOnWritesOwnershipTests(unittest.TestCase):
    def test_a_write_records_ownership(self):
        """The strategy enabling the write is what makes restoring it ours."""
        fake = _FakeSelf(switch_state="off", strategy_enabled=True, owned=None)
        # Called directly: the full enabled path needs a plan result, and what
        # matters here is that the write marks the switch as ours.
        asyncio.run(fake._set_pv_switch(False))
        self.assertEqual(fake._pv_switch_owned, False)
        self.assertEqual(fake._pv_switch_state, False)
        self.assertEqual(fake.services.calls, [("switch", "turn_off", {"entity_id": PV_SWITCH})])

    def test_a_failed_write_claims_no_ownership(self):
        """A rejected service call must not make the switch ours to restore."""

        class _Boom(_FakeServices):
            async def async_call(self, domain, service, data, **kwargs):
                raise RuntimeError("emaldo offline")

        fake = _FakeSelf(switch_state="off", strategy_enabled=True, owned=None)
        fake.services = _Boom()
        fake.hass = SimpleNamespace(services=fake.services, states=fake.hass.states)
        asyncio.run(fake._set_pv_switch(False))
        self.assertIsNone(fake._pv_switch_owned)


if __name__ == "__main__":
    unittest.main()
