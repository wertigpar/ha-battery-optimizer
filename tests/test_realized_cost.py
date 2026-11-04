"""Realized-cost grid_charge accounting (D3, issue #26 comment 5932659688).

``grid_charge_kwh`` was accumulated by PLANNED slot action, so grid energy
that reached the battery in a slot the plan labelled ``idle`` or
``discharge`` fell through every branch and was counted nowhere. The
reported figure was therefore always exactly the planned-charge total,
which is how a real ~9.2 kWh overnight charge was reported as 4.02 kWh.
"""

from __future__ import annotations

from types import SimpleNamespace

from tests import load_sensor

_sensor = load_sensor()


def _rec(action, soc_delta, import_kwh, export_kwh=0.0):
    return {
        "action": action,
        "soc_delta": soc_delta,
        "import_kwh": import_kwh,
        "export_kwh": export_kwh,
    }


def _attrs(records, capacity=15.0):
    """Build the sensor without HA and read its attributes."""
    inst = _sensor.RealizedCostTodaySensor.__new__(
        _sensor.RealizedCostTodaySensor
    )
    inst.coordinator = SimpleNamespace(
        realized_cost_today={"buy": 1.0, "sell": 0.5, "net": 0.5},
        realized_cost_today_records=records,
        config={_sensor.CONF_BATTERY_CAPACITY_KWH: capacity},
    )
    return inst.extra_state_attributes


def test_grid_charge_counts_import_in_idle_slot():
    """A slot the plan called 'idle' that still drew grid power is grid charge.

    The pre-fix code returned 0.0 here: the slot was labelled "idle", so it
    never reached the branch that accumulated import.
    """
    a = _attrs([_rec("idle", 6.0, 1.5)])
    assert a["grid_charge_kwh"] == 1.5


def test_grid_charge_counts_import_in_slot_labelled_discharge():
    """The plan label is not evidence: a "discharge" slot that actually rose.

    Same plan/meter mismatch as issue #26, with "discharge" in place of
    "idle". The pre-fix code returned 0.0 here for the same reason.
    """
    a = _attrs([_rec("discharge", 4.0, 0.8)])
    assert a["grid_charge_kwh"] == 0.8
    assert a["discharge_kwh"] == 0.0  # the SoC rose, nothing was released


def test_import_without_a_soc_rise_is_not_battery_charge():
    """Import in a slot where the battery gained nothing is house load.

    ``ik > 0.001`` with ``sd_kwh <= 0``: the meter saw the grid deliver,
    but the battery did not take it. Booking that as grid charge
    over-counts every non-charging slot in the day, since import is
    dominated by house load -- so ``grid_charge_kwh`` must stay strictly
    below the raw ``import_kwh``.
    """
    a = _attrs([_rec("discharge", -4.0, 0.8)])
    assert a["grid_charge_kwh"] == 0.0
    assert a["import_kwh"] == 0.8
    assert a["grid_charge_kwh"] < a["import_kwh"]
    assert a["discharge_kwh"] == 0.6  # 4 % of 15 kWh


def test_reported_shape_grid_charge_exceeds_planned_charge_slots():
    """Issue #26 shape: 4.02 kWh reported against a ~9.2 kWh overnight charge.

    Three planned charge slots carry 4.02 kWh. The rest of the charge
    lands in slots the plan called "idle". Before the fix those slots
    contributed to nothing at all, so the reported figure was exactly the
    planned-charge total -- the 4.02 the reporter saw.
    """
    records = [_rec("charge", 10.0, 1.34) for _ in range(3)]
    records += [_rec("idle", 8.0, 2.59) for _ in range(2)]
    a = _attrs(records, capacity=15.0)
    assert abs(a["grid_charge_kwh"] - 9.2) < 1e-6
    assert a["grid_charge_kwh"] > 4.02
    # The idle slots rose, so idle_drain is untouched (it only counts a fall).
    assert a["idle_drain_kwh"] == 0.0