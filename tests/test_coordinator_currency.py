"""The coordinator's ``currency`` must be a working property (live bug v0.3.27 rc).

A doubled ``@property`` made every read raise ``'property' object is not
callable``, which broke sensor platform setup and the locale repair check at
startup. Also covers ``currency_info`` (feeds the Active Currency sensor).
"""
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import tests

coord_mod = tests.load_coordinator()[0]
CoordCls = coord_mod.BatteryOptimizerCoordinator


def test_currency_reads_from_an_instance():
    inst = object.__new__(CoordCls)  # class-level _locale default (EUR)
    assert inst.currency == "EUR"


def test_currency_follows_the_instance_locale():
    inst = object.__new__(CoordCls)
    inst._locale = coord_mod.locale_money(
        override="", ha_currency="SEK", time_zone="Europe/Stockholm", country="SE"
    )
    assert inst.currency == "SEK"


def _inst(options, ha_currency, tz="Europe/Helsinki", country="FI"):
    inst = object.__new__(CoordCls)
    inst.config_entry = types.SimpleNamespace(options=options, data={})
    inst.hass = types.SimpleNamespace(
        config=types.SimpleNamespace(
            currency=ha_currency, time_zone=tz, country=country
        )
    )
    return inst


def test_currency_info_without_override_reports_home_assistant():
    info = _inst({}, "EUR").currency_info
    assert info["source"] == "home_assistant"
    assert info["user_override"] is None
    assert info["home_assistant_currency"] == "EUR"
    assert info["time_zone"] == "Europe/Helsinki"


def test_currency_info_with_override_reports_override():
    info = _inst({"currency": "SEK"}, "EUR").currency_info
    assert info["source"] == "override"
    assert info["user_override"] == "SEK"
