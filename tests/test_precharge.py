"""Speculative grid pre-charge slot selection (Strategy B).

Covers the pure selector ``speculative_precharge_slots``:

* Case 1 — cheap window closes *before* the ~14:00 publish → fire.
* Case 2 — cheap window extends past publish (horizon=1) → defer (empty).
* Case 3 — horizon=2 → fire even past publish (day+2 hedge).
* Already at/above safety SoC → skip.
* Good-solar day with require_low_solar → skip.
* Tomorrow prices already known + horizon=1 → skip (normal optimizer handles it).
* Day+2 hedge (horizon=2, tomorrow known): fire when tomorrow's optimized plan
  ends below safety and tonight has cheap grid energy; skip when tomorrow ends
  safe, when end-SoC is unknown, or when tonight is expensive.
* Negative spot (very cheap) → fire, energy capped by max_kwh_frac.
"""
from custom_components.battery_optimizer.optimizer import (
    BatteryConfig,
    speculative_precharge_slots,
)
from custom_components.battery_optimizer.const import PUBLISH_CUTOFF_SLOT

CHEAP = 0.05
EXPENSIVE = 0.20
CEILING = 0.07


def _cfg() -> BatteryConfig:
    return BatteryConfig(
        capacity_kwh=10.0,
        max_charge_kw=2.5,
        max_discharge_kw=2.5,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        soc_min=10.0,
        soc_max=90.0,
        base_load_kw=0.3,
    )


def _prices(cheap_range: tuple[int, int] | None) -> list[float]:
    p = [EXPENSIVE] * 96
    if cheap_range:
        for s in range(*cheap_range):
            p[s] = CHEAP
    return p


def _call(buy, solar, soc, **kw):
    cfg = _cfg()
    base = dict(
        buy_prices=buy,
        solar_15min=solar,
        cfg=cfg,
        current_soc_pct=soc,
        now_slot=0,
        tomorrow_available=False,
        horizon_days=1,
        safety_soc_frac=0.70,
        price_ceiling=CEILING,
        max_kwh_frac=0.40,
        low_solar_frac=0.25,
        require_low_solar=True,
    )
    base.update(kw)
    return speculative_precharge_slots(**base)


WEAK_SOLAR = [0.0] * 96
GOOD_SOLAR = [0.2] * 96  # 0.2 kW * 96 * 0.25h = 4.8 kWh >> band*0.25


def test_case1_fire_before_publish():
    """Cheap slots [40,56) close before slot 56 → pre-charge fires."""
    slots = _call(_prices((40, 56)), WEAK_SOLAR, 20.0)
    assert slots, "expected speculative slots"
    assert max(slots) < PUBLISH_CUTOFF_SLOT
    # cheapest 6 slots (energy-capped) within the cheap window
    assert min(slots) >= 40


def test_case2_defer_past_publish():
    """Cheap window extends past publish (horizon=1) → defer (no waste)."""
    slots = _call(_prices((50, 61)), WEAK_SOLAR, 20.0)
    assert slots == set()


def test_case3_horizon2_fires_past_publish():
    """horizon=2 → fire even when cheap window passes publish."""
    slots = _call(_prices((50, 61)), WEAK_SOLAR, 20.0, horizon_days=2)
    assert slots, "horizon=2 should hedge regardless of publish"


def test_skip_when_soc_at_safety():
    """Already at/above safety target → nothing to do."""
    slots = _call(_prices((40, 56)), WEAK_SOLAR, 66.0)
    assert slots == set()


def test_skip_good_solar_when_required():
    """Strong solar + require_low_solar → no pre-charge."""
    slots = _call(_prices((40, 56)), GOOD_SOLAR, 20.0)
    assert slots == set()


def test_fire_good_solar_when_not_required():
    """require_low_solar=False → weak/strong solar both fine."""
    slots = _call(_prices((40, 56)), GOOD_SOLAR, 20.0,
                  require_low_solar=False)
    assert slots, "should fire when solar gate disabled"


def test_skip_when_tomorrow_known():
    """Tomorrow prices available → normal optimizer handles it."""
    slots = _call(_prices((40, 56)), WEAK_SOLAR, 20.0,
                  tomorrow_available=True)
    assert slots == set()


def test_negative_spot_capped():
    """Negative spot in a before-publish window is 'cheap' but capped."""
    buy = [EXPENSIVE] * 96
    for s in range(40, 56):
        buy[s] = -0.02
    slots = _call(buy, WEAK_SOLAR, 20.0)
    assert slots, "negative spot is cheap → pre-charge"
    # max_kwh_frac caps energy → limited slot count, not all 96
    assert len(slots) < 96
    assert max(slots) < PUBLISH_CUTOFF_SLOT  # window closes before publish


def test_day2_hedge_fires_when_tomorrow_low():
    """horizon=2 + tomorrow known + tomorrow plan ends low + tonight cheap → hedge."""
    slots = _call(_prices((0, 96)), WEAK_SOLAR, 20.0,
                  tomorrow_available=True, horizon_days=2,
                  tomorrow_end_soc_pct=30.0)
    assert slots, "day+2 hedge should fire"
    # gentle cap: max_kwh_frac=0.40 → ~3.2 kWh → ≤ 6 slots
    assert len(slots) <= 6
    assert min(slots) >= 0


def test_day2_hedge_skips_when_tomorrow_full():
    """Tomorrow plan ends at/above safety → no hedge needed."""
    slots = _call(_prices((0, 96)), WEAK_SOLAR, 20.0,
                  tomorrow_available=True, horizon_days=2,
                  tomorrow_end_soc_pct=70.0)
    assert slots == set()


def test_day2_hedge_skips_when_end_unknown():
    """tomorrow_end_soc_pct=None (assessment impossible) → no hedge."""
    slots = _call(_prices((0, 96)), WEAK_SOLAR, 20.0,
                  tomorrow_available=True, horizon_days=2,
                  tomorrow_end_soc_pct=None)
    assert slots == set()


def test_day2_hedge_no_cheap_no_fire():
    """horizon=2 + tomorrow low but tonight expensive → no hedge."""
    slots = _call(_prices(None), WEAK_SOLAR, 20.0,
                  tomorrow_available=True, horizon_days=2,
                  tomorrow_end_soc_pct=30.0)
    assert slots == set()
