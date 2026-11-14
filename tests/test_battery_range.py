"""Issue #42: contract reserves and the Battery Range marker.

Pure-module tests: no Home Assistant imports.
"""
import tests

battery_range, _const = tests.load_battery_range()

BAND = battery_range.effective_soc_band


def test_flexible_contract_band_is_unchanged():
    assert BAND(
        capacity_kwh=14.8, soc_min=7, soc_max=100,
        reserved_bottom_kwh=0.0, reserved_top_kwh=0.0,
    ) == (7.0, 100.0)


def test_bottom_reserve_raises_the_floor():
    eff_min, eff_max = BAND(
        capacity_kwh=14.8, soc_min=7, soc_max=100,
        reserved_bottom_kwh=3.0, reserved_top_kwh=3.0,
    )
    assert round(eff_min, 2) == 20.27
    # 3 kWh of 14.8 kWh is 20.27 %, so the top reserve lowers the ceiling even
    # though soc_max is already 100.
    assert round(eff_max, 2) == 79.73


def test_top_reserve_lowers_the_ceiling():
    eff_min, eff_max = BAND(
        capacity_kwh=14.8, soc_min=20, soc_max=100,
        reserved_bottom_kwh=0.0, reserved_top_kwh=3.0,
    )
    assert eff_min == 20.0
    assert round(eff_max, 2) == 79.73


def test_reserve_below_the_configured_floor_does_not_move_it():
    eff_min, _ = BAND(
        capacity_kwh=14.8, soc_min=31, soc_max=100,
        reserved_bottom_kwh=3.0, reserved_top_kwh=0.0,
    )
    assert eff_min == 31.0


def test_zero_capacity_leaves_the_band_alone():
    assert BAND(
        capacity_kwh=0.0, soc_min=20, soc_max=100,
        reserved_bottom_kwh=3.0, reserved_top_kwh=3.0,
    ) == (20.0, 100.0)


def test_unset_marker_follows_the_planned_floor():
    assert battery_range.resolve_low_marker(13.0, None) == 13


def test_configured_marker_is_used_verbatim():
    assert battery_range.resolve_low_marker(31.0, 7) == 7


def test_marker_is_rounded_and_bounded():
    assert battery_range.resolve_low_marker(0.0, None) == 1
    assert battery_range.resolve_low_marker(12.6, None) == 13
    assert battery_range.resolve_low_marker(20.0, 0) == 1
    assert battery_range.resolve_low_marker(20.0, 150) == 99


def test_marker_is_clamped_to_the_high_marker():
    assert battery_range.clamp_marker_to_high(60, 50) == 50
    assert battery_range.clamp_marker_to_high(60, None) == 60
    assert battery_range.clamp_marker_to_high(40, 50) == 40


def test_marker_clamp_never_emits_an_invalid_pair():
    # A high marker of 0 is the idle byte / below the valid window; it must
    # not drag the low marker down with it.
    assert battery_range.clamp_marker_to_high(1, 0) == 1
    assert battery_range.clamp_marker_to_high(0, None) == 1
    assert battery_range.clamp_marker_to_high(-5, None) == 1
    assert battery_range.clamp_marker_to_high(500, 500) == 99
    assert battery_range.clamp_marker_to_high(1, 0) >= battery_range.MARKER_MIN


def test_marker_resolution_falls_back_when_the_setting_is_not_numeric():
    assert battery_range.resolve_low_marker(31.0, "not-a-number") == 31
    assert battery_range.resolve_low_marker(31.0, None) == 31


def test_reserved_error_rejects_an_empty_band():
    # floor 70 % is untouched; the top reserve pulls the ceiling to
    # 100 - 60 % = 40 %, so the top is the side that caused the empty band.
    err = battery_range.reserved_capacity_error(
        capacity_kwh=10.0, soc_min=70, soc_max=100,
        reserved_bottom_kwh=4.0, reserved_top_kwh=6.0,
    )
    assert err == ("top", "invalid_reserved_band")


def test_reserved_error_attributes_a_bottom_only_empty_band():
    # floor 0 % is moved up to the 40 % bottom reserve; the ceiling stays 30 %.
    err = battery_range.reserved_capacity_error(
        capacity_kwh=10.0, soc_min=0, soc_max=30,
        reserved_bottom_kwh=4.0, reserved_top_kwh=0.0,
    )
    assert err == ("bottom", "invalid_reserved_band")


def test_reserved_error_attributes_an_invalid_soc_band_to_base():
    # Zero reserves and Min SoC at/above Max SoC is a base-field problem.
    err = battery_range.reserved_capacity_error(
        capacity_kwh=10.0, soc_min=80, soc_max=50,
        reserved_bottom_kwh=0.0, reserved_top_kwh=0.0,
    )
    assert err == ("base", "invalid_soc_band")


def test_reserved_error_with_zero_capacity_and_a_positive_reserve():
    err = battery_range.reserved_capacity_error(
        capacity_kwh=0.0, soc_min=20, soc_max=100,
        reserved_bottom_kwh=3.0, reserved_top_kwh=0.0,
    )
    assert err == ("bottom", "reserved_exceeds_capacity")


def test_negative_reserves_are_floored_at_zero():
    eff_min, eff_max = BAND(
        capacity_kwh=14.8, soc_min=7, soc_max=100,
        reserved_bottom_kwh=-3.0, reserved_top_kwh=-3.0,
    )
    assert (eff_min, eff_max) == (7.0, 100.0)


def test_reserved_error_rejects_a_reserve_above_capacity():
    err = battery_range.reserved_capacity_error(
        capacity_kwh=10.0, soc_min=0, soc_max=100,
        reserved_bottom_kwh=11.0, reserved_top_kwh=0.0,
    )
    assert err == ("bottom", "reserved_exceeds_capacity")
    err = battery_range.reserved_capacity_error(
        capacity_kwh=10.0, soc_min=0, soc_max=100,
        reserved_bottom_kwh=0.0, reserved_top_kwh=11.0,
    )
    assert err == ("top", "reserved_exceeds_capacity")


def test_reserved_error_accepts_a_valid_guaranteed_contract():
    assert battery_range.reserved_capacity_error(
        capacity_kwh=14.8, soc_min=31, soc_max=100,
        reserved_bottom_kwh=3.0, reserved_top_kwh=3.0,
    ) is None
    assert battery_range.reserved_capacity_error(
        capacity_kwh=14.8, soc_min=7, soc_max=100,
        reserved_bottom_kwh=0.0, reserved_top_kwh=0.0,
    ) is None
