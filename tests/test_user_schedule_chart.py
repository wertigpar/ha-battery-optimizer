from __future__ import annotations

import unittest
from types import SimpleNamespace

from tests import load_sensor


class _FakeSlot:
    __slots__ = ("index", "action", "slot_value")

    def __init__(self, index, action="idle", slot_value=0):
        self.index = index
        self.action = action
        self.slot_value = slot_value


def _make_result(num_slots=96):
    return SimpleNamespace(
        slots=[_FakeSlot(i) for i in range(num_slots)],
        thirdparty_pv_slots=[False] * num_slots,
        slot_values=[0] * num_slots,
    )


def _make_sensor(last_sources):
    sensor = load_sensor()
    coord = SimpleNamespace(
        last_sources=last_sources,
        last_user_winners=None,
        last_pv_sources=None,
        last_sources_tomorrow=None,
        last_user_winners_tomorrow=None,
        last_pv_sources_tomorrow=None,
        last_result_tomorrow=None,
        last_result=_make_result(),
    )
    inst = sensor.UserScheduleChartSensor.__new__(sensor.UserScheduleChartSensor)
    inst.coordinator = coord
    return inst


class TestUserScheduleChart(unittest.TestCase):
    def test_no_user_rule_emits_full_baseline(self):
        """Issue: card showed 'loading…' on schedule: [].

        When only the default (optimizer) rule exists, last_sources is None.
        The chart must still receive the full 96-slot baseline (all source
        'optimizer') so it renders an empty-of-overrides timeline instead of a
        loading placeholder.  State stays 'no_schedule'.
        """
        inst = _make_sensor(last_sources=None)
        attrs = inst.extra_state_attributes
        sched = attrs["schedule"]
        self.assertEqual(len(sched), 96)
        self.assertTrue(all(s["source"] == "optimizer" for s in sched))
        self.assertTrue(all(s["soc_target"] is None for s in sched))
        self.assertEqual(inst.native_value, "no_schedule")

    def test_user_rule_emits_user_source(self):
        inst = _make_sensor(last_sources=["user"] * 96)
        attrs = inst.extra_state_attributes
        sched = attrs["schedule"]
        self.assertEqual(len(sched), 96)
        self.assertTrue(all(s["source"] == "user" for s in sched))
        self.assertNotEqual(inst.native_value, "no_schedule")


if __name__ == "__main__":
    unittest.main()
