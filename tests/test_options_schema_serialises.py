"""The options schema must serialise for the frontend (issues #21, #29).

The first attempt at #21 made the blank publish hour validate with a
three-member ``vol.Any``; that validated fine in Python and then failed in
``voluptuous_serialize.convert``, the step Home Assistant runs to send the
form to the browser, so the options page could not render at all and the
change was reverted before release.  This test runs that exact step over
the real schema so a selector that validates but cannot be serialised can
never ship again.  Needs a real Home Assistant install (skipped otherwise).
"""
from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
voluptuous_serialize = pytest.importorskip("voluptuous_serialize")

from homeassistant.helpers import config_validation as cv  # noqa: E402

from custom_components.battery_optimizer import config_flow, const  # noqa: E402
from custom_components.battery_optimizer.money import locale_money  # noqa: E402

MONEY = locale_money(
    override="", ha_currency="EUR", time_zone="Europe/Helsinki", country="FI"
)


def _serialise(defaults):
    schema = config_flow._build_schema(
        defaults, emaldo_options={"entry_1": "Power Store"}, money=MONEY
    )
    return voluptuous_serialize.convert(schema, custom_serializer=cv.custom_serializer)


def test_fresh_install_schema_serialises():
    fields = {f["name"]: f for f in _serialise(None)}
    assert const.CONF_PRECHARGE_PUBLISH_HOUR in fields
    assert fields[const.CONF_PRECHARGE_PUBLISH_HOUR]["selector"] == {
        "number": {"min": 0.0, "max": 23.0, "mode": "box"}
    }
    # The #31 options are present with their defaults.
    assert fields[const.CONF_CHEAP_CHARGE_ENABLED]["default"] is False
    assert fields[const.CONF_CHEAP_CHARGE_PRICE_THRESHOLD]["default"] == pytest.approx(
        MONEY.precharge_price_ceiling * const.DEFAULT_CHEAP_CHARGE_THRESHOLD_FRACTION
    )


def test_schema_with_a_blank_publish_hour_serialises():
    # An install that left the field empty (#29): the stored options carry
    # no value, the default is None, and the form must still render.
    fields = {f["name"]: f for f in _serialise({const.CONF_PRECHARGE_PUBLISH_HOUR: None})}
    assert fields[const.CONF_PRECHARGE_PUBLISH_HOUR]["selector"]["number"]["mode"] == "box"


def test_blank_publish_hour_validates_to_none():
    schema = config_flow._build_schema(
        None, emaldo_options={"entry_1": "Power Store"}, money=MONEY
    )
    validated = schema({const.CONF_PRECHARGE_PUBLISH_HOUR: ""})
    assert validated[const.CONF_PRECHARGE_PUBLISH_HOUR] is None
