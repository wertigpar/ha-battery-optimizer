"""runtime_state tests — serialize/validate/rebuild last-run state.

Pure-module tests (runtime_state.py): standalone importlib load — no
tests/__init__.py involvement, no HA imports, deterministic input.
"""

from __future__ import annotations

import collections
import json
import unittest
from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

_SRC = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "battery_optimizer"
    / "runtime_state.py"
)
_spec = spec_from_file_location("runtime_state", str(_SRC))
runtime_state = module_from_spec(_spec)
_spec.loader.exec_module(runtime_state)

_now = datetime(2026, 8, 8, 17, 0)

_FakeSlot = collections.namedtuple("_FakeSlot", "index action soc_after solar_kw")


def _valid_payload():
    slots = [
        _FakeSlot(50, "charge", 40.0, 0.5),
        _FakeSlot(52, "charge", 41.0, 0.9),
        _FakeSlot(53, "charge", 45.0, 1.2),
        _FakeSlot(55, "charge", 48.0, 1.5),
        _FakeSlot(90, "idle", 80.0, 0.0),
    ]
    plan = runtime_state.prune_plan_slots(slots, from_slot=53)
    return runtime_state.serialize_runtime(
        last_run_slot=53,
        last_run_initial_soc=45.0,
        last_run_scale=0.807,
        last_run_ts="2026-08-08T17:36:48+03:00",
        snapshot={"discharge": 2.3, "charge": 4.1, "solar": 12.5, "solar_ext": 3.2},
        plan_slots=plan,
    )


class PrunePlanSlotsTests(unittest.TestCase):
    def test_drops_slots_below_from_slot(self):
        slots = [
            _FakeSlot(50, "charge", 40.0, 0.5),
            _FakeSlot(52, "charge", 41.0, 0.9),
            _FakeSlot(53, "charge", 45.0, 1.2),
            _FakeSlot(55, "discharge", 48.0, 1.5),
            _FakeSlot(90, "idle", 80.0, 0.0),
        ]
        result = runtime_state.prune_plan_slots(slots, from_slot=53)
        self.assertEqual(
            result,
            [
                (53, "charge", 45.0, 1.2),
                (55, "discharge", 48.0, 1.5),
                (90, "idle", 80.0, 0.0),
            ],
        )
        self.assertTrue(all(isinstance(entry, tuple) for entry in result))

    def test_skips_missing_attribute_entries(self):
        slots = [
            _FakeSlot(50, "charge", 40.0, 0.5),
            SimpleNamespace(index=53, soc_after=45.0, solar_kw=1.2),  # no .action
            _FakeSlot(54, "charge", 46.0, 1.3),
            SimpleNamespace(index=56, action="idle", solar_kw=0.0),  # no .soc_after
            _FakeSlot(57, "idle", 50.0, 0.0),
        ]
        result = runtime_state.prune_plan_slots(slots, from_slot=50)
        self.assertEqual(
            result,
            [
                (50, "charge", 40.0, 0.5),
                (54, "charge", 46.0, 1.3),
                (57, "idle", 50.0, 0.0),
            ],
        )

    def test_skips_bool_index(self):
        slots = [
            SimpleNamespace(index=True, action="charge", soc_after=41.0, solar_kw=0.9),
            _FakeSlot(0, "charge", 42.0, 1.0),
        ]
        result = runtime_state.prune_plan_slots(slots, from_slot=0)
        self.assertEqual(result, [(0, "charge", 42.0, 1.0)])

    def test_skips_non_int_index(self):
        slots = [
            SimpleNamespace(index=53.5, action="charge", soc_after=45.0, solar_kw=1.2),
            _FakeSlot(53, "charge", 45.0, 1.2),
        ]
        result = runtime_state.prune_plan_slots(slots, from_slot=53)
        self.assertEqual(result, [(53, "charge", 45.0, 1.2)])

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(runtime_state.prune_plan_slots([], from_slot=0), [])


class SerializeRebuildRoundTripTests(unittest.TestCase):
    def test_json_round_trip(self):
        payload = _valid_payload()
        parsed = json.loads(json.dumps(payload))
        result = runtime_state.rebuild_runtime(parsed, _now)
        self.assertIsNotNone(result)
        self.assertEqual(result["last_run_slot"], 53)
        self.assertEqual(result["last_run_initial_soc"], 45.0)
        self.assertAlmostEqual(result["last_run_scale"], 0.807)
        self.assertEqual(result["last_run"], "2026-08-08T17:36:48+03:00")
        self.assertEqual(
            result["snapshot"],
            {"discharge": 2.3, "charge": 4.1, "solar": 12.5, "solar_ext": 3.2},
        )
        self.assertEqual(
            result["plan_slots"],
            [
                (53, "charge", 45.0, 1.2),
                (55, "charge", 48.0, 1.5),
                (90, "idle", 80.0, 0.0),
            ],
        )

    def test_rebuild_returns_plan_slots_as_tuples(self):
        payload = _valid_payload()
        parsed = json.loads(json.dumps(payload))
        result = runtime_state.rebuild_runtime(parsed, _now)
        self.assertIsNotNone(result)
        self.assertTrue(all(isinstance(entry, tuple) for entry in result["plan_slots"]))
        self.assertTrue(
            all(isinstance(entry[2], float) for entry in result["plan_slots"])
        )
        self.assertTrue(
            all(isinstance(entry[3], float) for entry in result["plan_slots"])
        )


class RebuildRejectsInvalidTests(unittest.TestCase):
    def test_non_dict(self):
        self.assertIsNone(runtime_state.rebuild_runtime("junk", _now))

    def test_missing_required_key(self):
        payload = _valid_payload()
        payload.pop("last_run_scale")
        self.assertIsNone(runtime_state.rebuild_runtime(payload, _now))

    def test_unparseable_last_run(self):
        payload = _valid_payload()
        payload["last_run"] = "not-a-date"
        self.assertIsNone(runtime_state.rebuild_runtime(payload, _now))

    def test_stale_day_rejected(self):
        payload = _valid_payload()
        payload["last_run"] = "2026-08-07T17:36:48+03:00"
        self.assertIsNone(runtime_state.rebuild_runtime(payload, _now))

    def test_empty_plan_slots(self):
        payload = _valid_payload()
        payload["plan_slots"] = []
        self.assertIsNone(runtime_state.rebuild_runtime(payload, _now))

    def test_plan_entry_wrong_length(self):
        payload = _valid_payload()
        payload["plan_slots"] = [[53, "charge", 40.0]]
        self.assertIsNone(runtime_state.rebuild_runtime(payload, _now))

    def test_plan_entry_non_numeric_soc_after(self):
        payload = _valid_payload()
        payload["plan_slots"] = [[53, "charge", "40.0", 0.0]]
        self.assertIsNone(runtime_state.rebuild_runtime(payload, _now))

    def test_plan_entry_bool_soc_after(self):
        payload = _valid_payload()
        payload["plan_slots"] = [[53, "charge", True, 0.0]]
        self.assertIsNone(runtime_state.rebuild_runtime(payload, _now))

    def test_plan_entry_non_int_index(self):
        payload = _valid_payload()
        payload["plan_slots"] = [[53.5, "charge", 40.0, 0.0]]
        self.assertIsNone(runtime_state.rebuild_runtime(payload, _now))

    def test_plan_entry_non_str_action(self):
        payload = _valid_payload()
        payload["plan_slots"] = [[53, 1, 40.0, 0.0]]
        self.assertIsNone(runtime_state.rebuild_runtime(payload, _now))

    def test_bool_initial_soc_rejected(self):
        payload = _valid_payload()
        payload["last_run_initial_soc"] = True
        self.assertIsNone(runtime_state.rebuild_runtime(payload, _now))

    def test_negative_last_run_slot(self):
        payload = _valid_payload()
        payload["last_run_slot"] = -1
        self.assertIsNone(runtime_state.rebuild_runtime(payload, _now))


class RebuildAcceptsTuplePlanSlotsTests(unittest.TestCase):
    def test_plan_slots_as_tuples(self):
        payload = _valid_payload()
        payload["plan_slots"] = [tuple(entry) for entry in payload["plan_slots"]]
        result = runtime_state.rebuild_runtime(payload, _now)
        self.assertIsNotNone(result)
        self.assertEqual(result["plan_slots"], payload["plan_slots"])


if __name__ == "__main__":
    unittest.main()
