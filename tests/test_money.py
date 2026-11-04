"""Locale-aware money helpers (pure, no Home Assistant imports).

``money.py`` uses a relative ``.const`` import, so it is exec'd into a
fabricated package whose ``const`` submodule is the real ``const.py``
(same bootstrap as ``tests/__init__.py``), keeping the no-HA requirement.
"""

from __future__ import annotations

import sys
import types
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_SRC_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "battery_optimizer"
)
_PKG_NAME = "bopt_money_pkg"

_pkg = types.ModuleType(_PKG_NAME)
_pkg.__path__ = []
sys.modules[_PKG_NAME] = _pkg


def _exec(name: str):
    spec = spec_from_file_location(f"{_PKG_NAME}.{name}", str(_SRC_DIR / f"{name}.py"))
    mod = module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_pkg.const = _exec("const")
_money = _exec("money")
_pkg.money = _money

EUR = _money.EUR
is_finland = _money.is_finland
resolve_currency = _money.resolve_currency
currency_from_unit = _money.currency_from_unit
LocaleMoney = _money.LocaleMoney
locale_money = _money.locale_money
ORIGIN_LITERALS = _money.ORIGIN_LITERALS
should_warn_mismatch = _money.should_warn_mismatch
currency_source = _money.currency_source
unreviewed_fields = _money.unreviewed_fields
DEFAULT_TRANSFER_FEE_BUY = 0.0776
DEFAULT_SALES_COMMISSION = 0.003


def test_override_wins_over_everything():
    assert resolve_currency(
        override="NOK", ha_currency="SEK", time_zone="Europe/Helsinki"
    ) == "NOK"


def test_ha_currency_wins_over_timezone():
    assert resolve_currency(
        override="", ha_currency="EUR", time_zone="Europe/Stockholm"
    ) == "EUR"


def test_timezone_is_the_fallback():
    assert resolve_currency(
        override=None, ha_currency=None, time_zone="Europe/Stockholm"
    ) == "SEK"


def test_unknown_timezone_yields_eur():
    assert resolve_currency(
        override="", ha_currency="", time_zone="America/New_York"
    ) == EUR


def test_empty_timezone_yields_eur():
    assert resolve_currency(override=None, ha_currency=None, time_zone="") == EUR


def test_values_are_normalised():
    assert resolve_currency(
        override="  sek ", ha_currency=None, time_zone=""
    ) == "SEK"


def test_finland_by_country():
    assert is_finland(country="FI", time_zone="Europe/Stockholm") is True


def test_finland_rejects_other_countries_even_in_helsinki():
    assert is_finland(country="SE", time_zone="Europe/Helsinki") is False


def test_finland_falls_back_to_timezone_without_country():
    assert is_finland(country="", time_zone="Europe/Helsinki") is True
    assert is_finland(country=None, time_zone="Europe/Stockholm") is False


_const = _pkg.const
_COUNTRY_VAT_MULTIPLIER = _const._COUNTRY_VAT_MULTIPLIER
_CURRENCY_MONETARY_DEFAULTS = _const._CURRENCY_MONETARY_DEFAULTS
DEFAULT_BATTERY_WEAR_COST = _const.DEFAULT_BATTERY_WEAR_COST
DEFAULT_FORCED_SELL_MIN_PROFIT = _const.DEFAULT_FORCED_SELL_MIN_PROFIT
DEFAULT_PRECHARGE_PRICE_CEILING = _const.DEFAULT_PRECHARGE_PRICE_CEILING
DEFAULT_SALES_COMMISSION = _const.DEFAULT_SALES_COMMISSION
DEFAULT_TRANSFER_FEE_BUY = _const.DEFAULT_TRANSFER_FEE_BUY
DEFAULT_VAT_MULTIPLIER = _const.DEFAULT_VAT_MULTIPLIER
is_tuned = _money.is_tuned
jurisdiction_defaults = _money.jurisdiction_defaults
monetary_defaults = _money.monetary_defaults
vat_multiplier_default = _money.vat_multiplier_default

MONETARY_KEYS = {
    "battery_wear_cost",
    "precharge_price_ceiling",
    "forced_sell_min_profit",
}


def test_every_currency_row_has_exactly_the_three_keys():
    for code, row in _CURRENCY_MONETARY_DEFAULTS.items():
        assert set(row) == MONETARY_KEYS, code


def test_the_four_nordic_currencies_are_all_tuned():
    for code in ("EUR", "SEK", "NOK", "DKK"):
        assert is_tuned(code) is True


def test_untuned_currency_reports_false():
    assert is_tuned("GBP") is False


def test_eur_row_equals_the_legacy_constants():
    row = _CURRENCY_MONETARY_DEFAULTS["EUR"]
    assert row["battery_wear_cost"] == DEFAULT_BATTERY_WEAR_COST
    assert row["precharge_price_ceiling"] == DEFAULT_PRECHARGE_PRICE_CEILING
    assert row["forced_sell_min_profit"] == DEFAULT_FORCED_SELL_MIN_PROFIT


def test_eur_row_equals_the_historical_literals():
    row = _CURRENCY_MONETARY_DEFAULTS["EUR"]
    assert row["battery_wear_cost"] == 0.03
    assert row["precharge_price_ceiling"] == 0.07
    assert row["forced_sell_min_profit"] == 0.02


def test_untuned_currency_falls_back_to_the_eur_values():
    assert monetary_defaults("GBP") == monetary_defaults("EUR")


def test_sek_wear_cost_is_rounded_to_a_human_number():
    assert monetary_defaults("SEK")["battery_wear_cost"] == 0.34


def test_monetary_defaults_returns_a_copy():
    first = monetary_defaults("SEK")
    first["battery_wear_cost"] = 999.0
    assert monetary_defaults("SEK")["battery_wear_cost"] == 0.34


def test_finnish_install_keeps_the_finnish_fees():
    row = jurisdiction_defaults(is_finland=True)
    assert row["transfer_fee_buy"] == DEFAULT_TRANSFER_FEE_BUY
    assert row["sales_commission"] == DEFAULT_SALES_COMMISSION


def test_non_finnish_install_gets_no_fabricated_fees():
    for finland in (False,):
        row = jurisdiction_defaults(is_finland=finland)
        assert row["transfer_fee_buy"] == 0.0
        assert row["sales_commission"] == 0.0


def test_vat_lookup_is_uppercased_and_whitespaced():
    assert vat_multiplier_default(country=" se ") == 1.25


def test_finland_vat_equals_the_legacy_constant():
    assert vat_multiplier_default(country="FI") == DEFAULT_VAT_MULTIPLIER


def test_nordic_vat_is_twenty_five_percent():
    for country in ("SE", "NO", "DK"):
        assert vat_multiplier_default(country=country) == 1.25


def test_unknown_country_gets_no_vat_multiplier():
    assert vat_multiplier_default(country="ZZ") == 1.0
    assert vat_multiplier_default(country=None) == 1.0
    assert vat_multiplier_default(country="") == 1.0


def test_finland_vat_and_derived_default_pin_the_historical_literals():
    assert _COUNTRY_VAT_MULTIPLIER["FI"] == 1.255
    assert DEFAULT_VAT_MULTIPLIER == 1.255


def test_legacy_defaults_pin_the_historical_literals():
    assert DEFAULT_BATTERY_WEAR_COST == 0.03
    assert DEFAULT_PRECHARGE_PRICE_CEILING == 0.07
    assert DEFAULT_FORCED_SELL_MIN_PROFIT == 0.02
    assert DEFAULT_TRANSFER_FEE_BUY == 0.0776
    assert DEFAULT_SALES_COMMISSION == 0.003


def test_finland_is_the_only_odd_vat_rate():
    assert _COUNTRY_VAT_MULTIPLIER["FI"] == 1.255
    non_finnish = {
        code: rate
        for code, rate in _COUNTRY_VAT_MULTIPLIER.items()
        if code != "FI"
    }
    assert 1.255 not in non_finnish.values()


def test_detects_a_currency_slash_kwh_unit():
    assert currency_from_unit("SEK/kWh") == "SEK"
    assert currency_from_unit("EUR/kWh") == "EUR"
    assert currency_from_unit("NOK/KWH") == "NOK"


def test_cents_form_units_are_skipped_not_guessed():
    assert currency_from_unit("snt/kWh") is None
    assert currency_from_unit("öre/kWh") is None
    assert currency_from_unit("øre/kWh") is None
    assert currency_from_unit("c/kWh") is None
    assert currency_from_unit("cent/kWh") is None


def test_empty_and_unrecognised_units_are_skipped():
    assert currency_from_unit("") is None
    assert currency_from_unit(None) is None
    assert currency_from_unit("kWh") is None
    assert currency_from_unit("kW") is None


def test_per_kwh_suffix_is_required():
    assert currency_from_unit("SEK") is None
    assert currency_from_unit("SEK/MWh") is None


def test_lowercase_input_is_uppercased():
    assert currency_from_unit("sek/kwh") == "SEK"


def test_ascii_denomination_spellings_are_skipped_not_guessed():
    for unit in (
        "cts/kWh",
        "ore/kWh",
        "ct/kWh",
        "cents/kWh",
    ):
        assert currency_from_unit(unit) is None, unit


def test_non_ascii_head_is_not_an_iso_code():
    assert currency_from_unit("ÄÖÜ/kWh") is None


def test_malformed_units_are_skipped():
    assert currency_from_unit("/kWh") is None
    assert currency_from_unit("SEK/") is None
    assert currency_from_unit("SEK/kWh/foo") is None


def test_three_letter_ascii_codes_are_accepted_without_an_allow_list():
    assert currency_from_unit("CZK/kWh") == "CZK"
    assert currency_from_unit("GBP/kWh") == "GBP"



def _swedish() -> LocaleMoney:
    return locale_money(
        override="",
        ha_currency="SEK",
        time_zone="Europe/Stockholm",
        country="SE",
    )


def test_swedish_install_gets_sek_and_swedish_numbers():
    loc = _swedish()
    assert loc.currency == "SEK"
    assert loc.vat_multiplier == 1.25
    assert loc.battery_wear_cost == 0.34
    assert loc.precharge_price_ceiling == 0.80
    assert loc.forced_sell_min_profit == 0.23
    assert loc.transfer_fee_buy == 0.0
    assert loc.sales_commission == 0.0


def test_finnish_install_keeps_the_finnish_fees():
    loc = locale_money(
        override="",
        ha_currency="EUR",
        time_zone="Europe/Helsinki",
        country="FI",
    )
    assert loc.currency == "EUR"
    assert loc.vat_multiplier == 1.255
    assert loc.transfer_fee_buy == DEFAULT_TRANSFER_FEE_BUY
    assert loc.sales_commission == DEFAULT_SALES_COMMISSION
    assert loc.battery_wear_cost == 0.03


def test_currency_override_does_not_move_vat():
    loc = locale_money(
        override="EUR",
        ha_currency="SEK",
        time_zone="Europe/Stockholm",
        country="SE",
    )
    assert loc.currency == "EUR"
    assert loc.vat_multiplier == 1.25


def test_untuned_currency_still_gets_the_country_vat():
    loc = locale_money(
        override="GBP",
        ha_currency="",
        time_zone="Europe/London",
        country="DE",
    )
    assert loc.currency == "GBP"
    assert loc.battery_wear_cost == 0.03
    assert loc.vat_multiplier == 1.19


def test_locale_money_is_immutable():
    loc = _swedish()
    try:
        loc.currency = "EUR"
    except Exception:
        return
    raise AssertionError("LocaleMoney should be frozen")





def test_mismatch_warns_when_the_sensor_disagrees():
    assert should_warn_mismatch(
        sensor_unit="EUR/kWh", currency="SEK", override=""
    ) is True


def test_no_warning_when_the_sensor_agrees():
    assert should_warn_mismatch(
        sensor_unit="SEK/kWh", currency="SEK", override=""
    ) is False


def test_no_warning_for_a_cents_unit():
    assert should_warn_mismatch(
        sensor_unit="snt/kWh", currency="SEK", override=""
    ) is False


def test_no_warning_when_the_user_has_set_an_override():
    assert should_warn_mismatch(
        sensor_unit="EUR/kWh", currency="EUR", override="EUR"
    ) is False


def test_no_warning_for_a_blank_sensor_unit():
    assert should_warn_mismatch(
        sensor_unit="", currency="SEK", override=""
    ) is False


def test_unreviewed_fields_flags_a_swedish_install_on_finnish_vat():
    loc = locale_money(
        override="", ha_currency="SEK", time_zone="Europe/Stockholm", country="SE"
    )
    stored = {"vat_multiplier": 1.255, "battery_wear_cost": 0.34}
    assert unreviewed_fields(stored, loc) == ["vat_multiplier"]


def test_unreviewed_fields_is_silent_for_a_correct_finnish_install():
    loc = locale_money(
        override="", ha_currency="EUR", time_zone="Europe/Helsinki", country="FI"
    )
    stored = dict(ORIGIN_LITERALS)
    assert unreviewed_fields(stored, loc) == []


def test_unreviewed_fields_flags_untouched_currency_defaults():
    loc = locale_money(
        override="", ha_currency="SEK", time_zone="Europe/Stockholm", country="SE"
    )
    stored = {"battery_wear_cost": 0.03, "precharge_price_ceiling": 0.07}
    assert unreviewed_fields(stored, loc) == [
        "precharge_price_ceiling",
        "battery_wear_cost",
    ]


def test_unreviewed_fields_ignores_touched_values():
    loc = locale_money(
        override="", ha_currency="SEK", time_zone="Europe/Stockholm", country="SE"
    )
    stored = {"battery_wear_cost": 0.30}
    assert unreviewed_fields(stored, loc) == []


def test_unreviewed_fields_ignores_missing_and_unparseable_values():
    loc = locale_money(
        override="", ha_currency="SEK", time_zone="Europe/Stockholm", country="SE"
    )
    assert unreviewed_fields({}, loc) == []
    assert unreviewed_fields({"battery_wear_cost": None}, loc) == []


def test_currency_source_names_the_winning_input():
    assert currency_source(
        override="nok", ha_currency="SEK", time_zone="Europe/Helsinki"
    ) == "override"
    assert currency_source(
        override="", ha_currency="EUR", time_zone="Europe/Stockholm"
    ) == "home_assistant"
    assert currency_source(
        override=None, ha_currency=None, time_zone="Europe/Stockholm"
    ) == "timezone"
    assert currency_source(
        override=None, ha_currency=" ", time_zone="Europe/Helsinki"
    ) == "default"


def test_currency_source_is_override_only_when_one_is_set():
    for ov, ha, tz in [("", "", "Europe/Oslo"), ("dkk", "", ""), ("", "sek", "Europe/Oslo")]:
        src = currency_source(override=ov, ha_currency=ha, time_zone=tz)
        assert (src == "override") == bool(ov.strip())
