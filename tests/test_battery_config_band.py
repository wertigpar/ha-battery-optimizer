"""Issue #42: reserves narrow the band BatteryConfig is built with."""
import logging
import types

import tests

mod = tests.load_coordinator()[0]
C = mod.BatteryOptimizerCoordinator


def _inst(**overrides):
    o = object.__new__(C)
    o._entry = types.SimpleNamespace(
        data={
            mod.CONF_BATTERY_CAPACITY_KWH: 14.8,
            mod.CONF_SOC_MIN: 7,
            mod.CONF_SOC_MAX: 100,
            mod.CONF_MAX_CHARGE_KW: 10.0,
            mod.CONF_MAX_DISCHARGE_KW: 10.0,
            **overrides,
        },
        options={},
    )
    o._auto_base_load_value = None
    o._band_clamp_warning = None
    o._locale = types.SimpleNamespace(
        vat_multiplier=1.24,
        transfer_fee_buy=0.0,
        sales_commission=0.0,
        battery_wear_cost=0.03,
        forced_sell_min_profit=0.0,
    )
    return o


def test_flexible_contract_keeps_the_configured_band():
    cfg = _inst()._build_battery_config()
    assert cfg.soc_min == 7.0
    assert cfg.soc_max == 100.0


def test_bottom_reserve_raises_the_planned_floor():
    cfg = _inst(
        **{
            mod.CONF_RESERVED_BOTTOM_KWH: 3.0,
            mod.CONF_RESERVED_TOP_KWH: 3.0,
        }
    )._build_battery_config()
    assert round(cfg.soc_min, 2) == 20.27
    # the same 3 kWh at the top lowers the ceiling by 20.27 points
    assert round(cfg.soc_max, 2) == 79.73


def test_top_reserve_lowers_the_planned_ceiling():
    cfg = _inst(**{mod.CONF_RESERVED_TOP_KWH: 3.0})._build_battery_config()
    assert round(cfg.soc_max, 2) == 79.73


def test_band_clamp_warning_logs_once_per_reserve_set(caplog):
    inst = _inst(**{mod.CONF_RESERVED_BOTTOM_KWH: 3.0})
    with caplog.at_level(logging.WARNING, logger=mod._LOGGER.name):
        inst._build_battery_config()
        assert caplog.text.count("Reserved capacity clamps") == 1

        inst._build_battery_config()
        assert caplog.text.count("Reserved capacity clamps") == 1

        inst._entry.data[mod.CONF_RESERVED_TOP_KWH] = 3.0
        inst._build_battery_config()
        assert caplog.text.count("Reserved capacity clamps") == 2


def test_configured_marker_prefers_options_over_data():
    inst = _inst()
    inst._entry.data[mod.CONF_BATTERY_RANGE_LOW_MARKER] = 7.0
    inst._entry.options[mod.CONF_BATTERY_RANGE_LOW_MARKER] = 9.0
    assert inst._configured_low_marker() == 9.0
