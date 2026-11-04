import pytest

from . import load_coordinator, load_optimizer

opt, _const = load_optimizer()
coord, _coord_const = load_coordinator()

SLOTS_PER_DAY = 96
# Day-2 solar bell: peak 2.0 kW at slot 84, gaussian width 19 slots.  Same
# shape as the pre-#25 single-day plan so the horizon is the only variable.
SOLAR_PEAK_KW = 2.0
SOLAR_CENTRE_SLOT = 84
SOLAR_WIDTH_SLOTS = 19
# issue #25 fixture: day-1 midday is cheap (0.05) so the plan charges there,
# day-2 midday is dear (0.30) so the plan discharges the other 35 slots.
DAY1_BUY_PRICE = 0.05
DAY2_BUY_PRICE = 0.30
SELL_PRICE = 0.01
INITIAL_SOC_PCT = 50.0


def test_optimize_accepts_two_day_horizon():
    total_slots = 192
    buy_prices = [0.10] * total_slots
    sell_prices = [0.05] * total_slots
    solar_15min = [0.0] * total_slots
    cfg = opt.BatteryConfig()

    result = opt.optimize(
        buy_prices,
        sell_prices,
        solar_15min,
        cfg,
        initial_soc_pct=50.0,
        enable_night_drain=False,
        total_slots=total_slots,
    )

    assert len(result.slots) == total_slots
    assert result.remaining_slots == total_slots


def test_two_day_horizon_plans_second_day():
    """day 2 receives a non-none plan when day-2 prices are cheaper, i.e. the 192-slot horizon is not truncated to 96."""
    total_slots = 192
    buy_prices = [0.20] * 96 + [0.01] * 96
    sell_prices = [0.01] * total_slots
    solar_15min = [0.0] * total_slots
    cfg = opt.BatteryConfig(
        transfer_fee_buy=0.0,
        sales_commission=0.0,
        vat_multiplier=1.0,
        enable_soc_safeguard=False,
        enable_night_drain=False,
    )

    result = opt.optimize(
        buy_prices,
        sell_prices,
        solar_15min,
        cfg,
        initial_soc_pct=50.0,
        enable_night_drain=False,
        total_slots=total_slots,
    )

    assert any(slot.action == "charge" for slot in result.slots[96:])
    # Export/discharge is not exercised because this fixture has no positive export arbitrage.


def test_two_day_horizon_respects_start_slot():
    total_slots = 192
    buy_prices = [
        0.20 if slot in (68, 164) else 0.01 + (slot % 4) * 0.003
        for slot in range(total_slots)
    ]
    sell_prices = [0.01 + (slot % 3) * 0.002 for slot in range(total_slots)]
    solar_15min = [0.0] * total_slots
    cfg = opt.BatteryConfig(
        transfer_fee_buy=0.0,
        sales_commission=0.0,
        vat_multiplier=1.0,
        enable_soc_safeguard=False,
        enable_night_drain=False,
    )

    result = opt.optimize(
        buy_prices,
        sell_prices,
        solar_15min,
        cfg,
        initial_soc_pct=100.0,
        start_slot=68,
        enable_night_drain=False,
        total_slots=total_slots,
    )

    assert all(slot.action == "none" for slot in result.slots[:68])
    assert any(slot.action != "none" for slot in result.slots[68:])
    assert result.remaining_slots == 124


def test_two_day_horizon_sizes_all_per_slot_lists():
    total_slots = 192
    buy_prices = [
        0.20 if slot in (164, 191) else 0.01 + (slot % 4) * 0.003
        for slot in range(total_slots)
    ]
    sell_prices = [0.01 + (slot % 3) * 0.002 for slot in range(total_slots)]
    solar_15min = [0.0] * total_slots
    cfg = opt.BatteryConfig(
        transfer_fee_buy=0.0,
        sales_commission=0.0,
        vat_multiplier=1.0,
        enable_soc_safeguard=False,
        enable_night_drain=False,
    )

    result = opt.optimize(
        buy_prices,
        sell_prices,
        solar_15min,
        cfg,
        initial_soc_pct=20.0,
        enable_night_drain=False,
        enable_pv_strategy=True,
        total_slots=total_slots,
    )

    assert len(result.emaldo_modes) == total_slots
    assert len(result.thirdparty_pv_slots) == total_slots
    assert isinstance(result.sell_slots, list)
    assert isinstance(result.safeguard_slots, list)


def test_default_horizon_preserves_action_list_and_profit():
    total_slots = 96
    buy_prices = [0.01 + (slot % 4) * 0.01 for slot in range(total_slots)]
    sell_prices = [0.002 + (slot % 3) * 0.001 for slot in range(total_slots)]
    solar_15min = [0.0] * total_slots
    cfg = opt.BatteryConfig(
        transfer_fee_buy=0.0,
        sales_commission=0.0,
        vat_multiplier=1.0,
        enable_soc_safeguard=False,
        enable_night_drain=False,
    )

    result = opt.optimize(
        buy_prices,
        sell_prices,
        solar_15min,
        cfg,
        initial_soc_pct=20.0,
        enable_night_drain=False,
    )

    assert [slot.action for slot in result.slots] == ["idle"] * total_slots
    assert round(result.total_profit, 6) == 0.0


@pytest.mark.parametrize("total_slots", [0, 10])
def test_invalid_total_slots_warns_and_falls_back_to_96(total_slots, caplog):
    buy_prices = [0.01 + (slot % 4) * 0.01 for slot in range(96)]
    sell_prices = [0.002 + (slot % 3) * 0.001 for slot in range(96)]
    solar_15min = [0.0] * 96
    cfg = opt.BatteryConfig(
        transfer_fee_buy=0.0,
        sales_commission=0.0,
        vat_multiplier=1.0,
        enable_soc_safeguard=False,
        enable_night_drain=False,
    )

    result = opt.optimize(
        buy_prices,
        sell_prices,
        solar_15min,
        cfg,
        initial_soc_pct=20.0,
        enable_night_drain=False,
        total_slots=total_slots,
    )

    assert len(result.slots) == 96
    assert result.remaining_slots == 96
    assert "total_slots must be a positive int >= 96" in caplog.text


def _day_solar_slots(total_slots, centre, width, peak):
    """Gaussian solar bell clipped to the horizon."""
    slots = [0.0] * total_slots
    for slot in range(centre - 5 * width, centre + 5 * width):
        if 0 <= slot < total_slots:
            slots[slot] = peak * 2 ** (-(((slot - centre) / width) ** 2))
    return slots


def _optimize_two_day():
    """192-slot horizon: cheap day-1 midday, dear day-2 midday, solar on day 2."""
    total_slots = 2 * SLOTS_PER_DAY
    buy_prices = [DAY1_BUY_PRICE] * SLOTS_PER_DAY + [DAY2_BUY_PRICE] * SLOTS_PER_DAY
    sell_prices = [SELL_PRICE] * total_slots
    solar_15min = _day_solar_slots(
        total_slots, SOLAR_CENTRE_SLOT, SOLAR_WIDTH_SLOTS, SOLAR_PEAK_KW
    )
    cfg = opt.BatteryConfig(
        transfer_fee_buy=0.0,
        sales_commission=0.0,
        vat_multiplier=1.0,
        enable_soc_safeguard=False,
        enable_night_drain=False,
    )
    return opt.optimize(
        buy_prices,
        sell_prices,
        solar_15min,
        cfg,
        initial_soc_pct=INITIAL_SOC_PCT,
        enable_pv_strategy=True,
        enable_night_drain=False,
        total_slots=total_slots,
    )


def test_split_result_by_day_reports_a_usable_second_day():
    """#25: the day-1 snapshot must leave day 2 its own totals, not zeros."""
    result = _optimize_two_day()

    today, tomorrow = coord._split_result_by_day(result)

    assert len(today.slots) == SLOTS_PER_DAY
    assert len(tomorrow.slots) == SLOTS_PER_DAY
    assert [slot.index for slot in today.slots] == list(range(SLOTS_PER_DAY))
    assert [slot.index for slot in tomorrow.slots] == list(range(SLOTS_PER_DAY))

    # Day 1 charges, day 2 discharges against the dear midday price.
    assert any(slot.action == "charge" for slot in today.slots)
    # Was pinned at exactly 35.  The over-commit floor guard used to answer
    # the second day's standby drain by deleting committed discharge, which
    # cost this plan eight discharge slots overall; it now funds the drain
    # instead and retains more of them, so day 2 now discharges 36.  The
    # exact number is not the point of this test (#25 is the day-split), so
    # it is held as a regression floor: a collapse back toward an empty day 2
    # still fails.  Measured on this fixture the guard change is strictly
    # better — total_profit 0.2791 -> 0.3455 and grid_import 16.395 ->
    # 15.452 kWh, with min SoC after the first discharge still above the 25 %
    # floor target (29.76 % -> 27.52 %).
    assert sum(slot.action == "discharge" for slot in tomorrow.slots) >= 35

    # These collapse to zero when the day-1 snapshot is missing.
    assert tomorrow.baseline_cost > 0
    assert tomorrow.net_profit != 0
    assert today.net_profit < 0 < tomorrow.net_profit

    assert result.day1_totals, "192-slot run must populate the day-1 snapshot"

    # Day 1 + day 2 reconstruct the continuous total for every split field.
    for field_name in coord._SPLIT_SCALAR_FIELDS:
        split = getattr(today, field_name) + getattr(tomorrow, field_name)
        assert split == pytest.approx(getattr(result, field_name), abs=1e-9), field_name


def test_split_result_by_day_day1_totals_are_not_inflated():
    """#25: day 1 must report its own totals, not the continuous two-day total."""
    result = _optimize_two_day()

    today, _tomorrow = coord._split_result_by_day(result)

    for field_name in (
        "baseline_cost",
        "baseline_import_kwh",
        "baseline_export_kwh",
        "grid_import_kwh",
        "grid_export_kwh",
        "grid_energy",
    ):
        day_value = getattr(today, field_name)
        assert day_value > 0, field_name
        assert day_value < getattr(result, field_name), field_name


def test_split_result_by_day_sums_a_synthetic_snapshot():
    """#25: a fully populated snapshot splits every scalar into its two days."""
    total_slots = 2 * SLOTS_PER_DAY
    slots = [
        opt.SlotPlan(index=slot, action="idle", slot_value=0, buy_price=0.1, sell_price=0.01)
        for slot in range(total_slots)
    ]
    # Distinct, nonzero day-1 value per field so a mix-up cannot pass.
    day1 = {
        field_name: (offset + 1) * (0.37 if isinstance(getattr(opt.OptimizationResult(), field_name), float) else 1)
        for offset, field_name in enumerate(coord._SPLIT_SCALAR_FIELDS)
    }
    result = opt.OptimizationResult(
        slots=slots,
        emaldo_modes=[0] * total_slots,
        thirdparty_pv_slots=[True] * total_slots,
        **{name: day1[name] * 2 for name in coord._SPLIT_SCALAR_FIELDS},
        day1_totals=dict(day1),
    )

    today, tomorrow = coord._split_result_by_day(result)

    for field_name in coord._SPLIT_SCALAR_FIELDS:
        assert getattr(today, field_name) == pytest.approx(day1[field_name], abs=1e-9), field_name
        assert getattr(tomorrow, field_name) == pytest.approx(day1[field_name], abs=1e-9), field_name
        assert getattr(today, field_name) + getattr(tomorrow, field_name) == pytest.approx(
            getattr(result, field_name), abs=1e-9
        ), field_name
    assert today.day1_totals is None
    assert tomorrow.day1_totals is None


def test_split_result_by_day_single_day_horizon_keeps_everything_on_day_one():
    """#25: a 96-slot run never crosses the boundary, so day 1 owns the totals."""
    total_slots = SLOTS_PER_DAY
    buy_prices = [0.10] * total_slots
    sell_prices = [0.01] * total_slots
    solar_15min = [0.0] * total_slots
    cfg = opt.BatteryConfig(
        transfer_fee_buy=0.0,
        sales_commission=0.0,
        vat_multiplier=1.0,
        enable_soc_safeguard=False,
        enable_night_drain=False,
    )

    result = opt.optimize(
        buy_prices,
        sell_prices,
        solar_15min,
        cfg,
        initial_soc_pct=50.0,
        enable_night_drain=False,
    )
    assert result.day1_totals is None

    today, tomorrow = coord._split_result_by_day(result)

    assert today.remaining_slots == result.remaining_slots
    for field_name in coord._SPLIT_SCALAR_FIELDS:
        assert getattr(today, field_name) == getattr(result, field_name), field_name
        assert getattr(tomorrow, field_name) == 0, field_name


# --- forced-sell day split (issue #25) ---------------------------------
# Forced-sell (manual sell) revenue/profit/target accumulate onto the result
# AFTER the OptimizationResult is constructed, so the day-1 snapshot has to be
# extended inside the forced-sell block.  If it is not, the coordinator finds
# no day-1 value for the three sell fields and dumps the whole continuous total
# on day 1 with day 2 at zero.
SELL_BAND = range(92, 106)  # high sell-price band: opens the window at slot 92


def _optimize_forced_sell_two_day():
    """192-slot horizon whose forced-sell window straddles the day boundary.

    Buy prices are flat and cheap, so the cheapest-forward reference buy is the
    same for every sell slot.  The sell-price band lifts the sell price above
    ``forced_sell_min_price`` from slot 92, opening the window four slots before
    the day-1 cut, and the battery budget carries it through slots 96-97 on day
    2 (usable = 20 kWh start SoC - 20% floor - 1 kWh reserve = 15 kWh = six
    2.5 kWh battery-tier slots).
    """
    total_slots = 2 * SLOTS_PER_DAY
    buy_prices = [0.05] * total_slots
    sell_prices = [0.30 if s in SELL_BAND else 0.01 for s in range(total_slots)]
    solar_15min = [0.0] * total_slots
    cfg = opt.BatteryConfig(
        capacity_kwh=20.0,
        transfer_fee_buy=0.0,
        sales_commission=0.0,
        vat_multiplier=1.0,
        enable_soc_safeguard=False,
        enable_night_drain=False,
        forced_sell_enabled=True,
        forced_sell_min_price=0.20,
        forced_sell_min_profit=0.02,
    )
    return opt.optimize(
        buy_prices,
        sell_prices,
        solar_15min,
        cfg,
        initial_soc_pct=100.0,
        enable_night_drain=False,
        total_slots=total_slots,
    )


def test_forced_sell_day_split_partitions_sell_totals_across_the_boundary():
    """#25: forced-sell sell_* totals split into two non-empty days."""
    result = _optimize_forced_sell_two_day()

    # The sell window must straddle the cut, otherwise the split is untested.
    day1_sell_slots = [s for s in result.sell_slots if s < SLOTS_PER_DAY]
    day2_sell_slots = [s for s in result.sell_slots if s >= SLOTS_PER_DAY]
    assert day1_sell_slots, "fixture must force-sell on day 1"
    assert day2_sell_slots, "fixture must force-sell on day 2 (boundary-crossing window)"

    # The day-1 snapshot handed to the result must carry the three sell keys.
    assert result.day1_totals is not None
    for key in ("sell_revenue", "sell_profit", "sell_target_kwh"):
        assert key in result.day1_totals, key

    today, tomorrow = coord._split_result_by_day(result)

    for field_name in ("sell_revenue", "sell_profit", "sell_target_kwh"):
        continuous = getattr(result, field_name)
        day1 = getattr(today, field_name)
        day2 = getattr(tomorrow, field_name)
        # Exact partition: the two days reconstruct the continuous total.
        assert day1 + day2 == pytest.approx(continuous, abs=1e-9), field_name
        # Genuine partition: both sides non-degenerate, neither owns the total.
        # A missing day-1 snapshot value would put the whole total on day 1 and
        # zero on day 2 — these assertions are what catch that.
        assert continuous > 0, field_name
        assert day1 > 0, field_name
        assert day2 > 0, field_name
        assert day1 < continuous, field_name

    # Cross-check day-1 kWh against the slot plan: the sum of battery-tier sell
    # kWh for the day-1 sell slots (result.sell_slots with s < 96).
    battery_day1_kwh = sum(
        result.slots[s].sell_kwh
        for s in result.sell_slots
        if s < SLOTS_PER_DAY and result.slots[s].sell_source == "battery"
    )
    assert battery_day1_kwh > 0
    assert today.sell_target_kwh == pytest.approx(battery_day1_kwh, abs=1e-9)
    assert today.sell_target_kwh < result.sell_target_kwh
