"""Coordinator wiring for ``solar_forecast_source`` (Emaldo-native PV forecast).

The planner reads PV through one method — `_get_solcast_forecast` — so the
whole feature is that method's source dispatch plus the Emaldo reader.  These
tests drive the real methods against a ``SimpleNamespace`` fake ``self`` (the
``load_coordinator`` harness pattern, cf. ``test_accuracy_skip.py``):

* source=``emaldo`` reads the unit's ``schedule_chart`` sensor and returns kW;
* the default (key absent — every existing install) still reads the Solcast
  shaped sensor and must not touch the Emaldo schedule sensor at all;
* an all-zero Emaldo forecast is warned about exactly once per run process,
  because "no PV" and "this unit does not expose PV" look identical;
* `_get_solar_confidence_ratio` reports "no confidence data" for the Emaldo
  source instead of looking up Solcast keys that such a config never stored.
"""

from __future__ import annotations

import logging
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from . import load_coordinator

coord, const = load_coordinator()
BatteryOptimizerCoordinator = coord.BatteryOptimizerCoordinator

SCHEDULE_SENSOR = "sensor.emaldo_schedule_chart"
SOLCAST_TODAY = "sensor.solcast_pv_forecast_forecast_today"
SOLCAST_TOMORROW = "sensor.solcast_pv_forecast_forecast_tomorrow"

# The harness stub pins HA "now" to 2026-01-01 and the local zone to +01:00.
DAY = date(2026, 1, 1)


class _FakeState:
    def __init__(self, attributes):
        self.attributes = attributes


class _FakeStates:
    def __init__(self, states):
        self._states = states
        self.looked_up = []

    def get(self, entity_id):
        self.looked_up.append(entity_id)
        return self._states.get(entity_id)


def _schedule(watts_per_slot: float):
    """One local day of schedule_chart slots, ``solar`` in watts."""
    base = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1)))
    return [
        {
            "t": (base + timedelta(minutes=index * 15)).isoformat(),
            "solar": watts_per_slot,
            "price": 100.0,
        }
        for index in range(const.SLOTS_PER_DAY)
    ]


class _FakeSelf:
    """Minimal ``self`` carrying the real methods under test.

    Only the attributes those three methods touch are provided — the rest of
    the coordinator (planning, pushing, HA lifecycle) is not involved in the
    source dispatch, and faking less is what keeps the test honest.
    """

    _get_solcast_forecast = BatteryOptimizerCoordinator._get_solcast_forecast
    _get_emaldo_solar_forecast = BatteryOptimizerCoordinator._get_emaldo_solar_forecast
    _get_solar_confidence_ratio = BatteryOptimizerCoordinator._get_solar_confidence_ratio
    _grid_charge_solar_aware_enabled = (
        BatteryOptimizerCoordinator._grid_charge_solar_aware_enabled
    )

    def __init__(self, config, states, solar_scale=1.0, resolve=SCHEDULE_SENSOR):
        self.config = config
        self.hass = SimpleNamespace(
            states=_FakeStates(states), config=SimpleNamespace(time_zone="Test/Zone")
        )
        self._solar_scale = solar_scale
        self._emaldo_solar_warned = False
        self._resolve_emaldo_entity = (
            (lambda key, domain="sensor": resolve)
            if resolve
            else (lambda key, domain="sensor": None)
        )


def _fake_self(config, states, solar_scale=1.0, resolve=SCHEDULE_SENSOR):
    return _FakeSelf(config, states, solar_scale, resolve)


def _emaldo_config(**extra):
    config = {const.CONF_SOLAR_FORECAST_SOURCE: const.SOLAR_SOURCE_EMALDO}
    config.update(extra)
    return config


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class TestSourceDispatch(unittest.TestCase):
    def test_emaldo_source_reads_the_schedule_chart_sensor(self):
        """source=emaldo: the unit's own watts become the planner's kW."""
        fake = _fake_self(
            _emaldo_config(),
            {SCHEDULE_SENSOR: _FakeState({"schedule": _schedule(2500.0)})},
        )
        forecast = fake._get_solcast_forecast("today")
        self.assertEqual(len(forecast), const.SLOTS_PER_DAY)
        self.assertTrue(all(abs(v - 2.5) < 1e-9 for v in forecast))
        self.assertIn(SCHEDULE_SENSOR, fake.hass.states.looked_up)

    def test_tomorrow_uses_tomorrow_of_the_same_schedule_window(self):
        fake = _fake_self(
            _emaldo_config(),
            {SCHEDULE_SENSOR: _FakeState({"schedule": _schedule(4000.0)})},
        )
        forecast = fake._get_emaldo_solar_forecast("tomorrow")
        # The window above is today only: tomorrow is unknown, not fabricated.
        self.assertEqual(sum(forecast), 0.0)

    def test_forecast_scale_multiplies_the_emaldo_source_too(self):
        fake = _fake_self(
            _emaldo_config(),
            {SCHEDULE_SENSOR: _FakeState({"schedule": _schedule(4000.0)})},
            solar_scale=0.5,
        )
        forecast = fake._get_solcast_forecast("today")
        self.assertAlmostEqual(sum(forecast), const.SLOTS_PER_DAY * 2.0, places=6)

    def test_default_source_never_touches_the_schedule_chart_sensor(self):
        """Key absent (every install predating this option) keeps Solcast."""
        flat = [{"pv_estimate": 3.0, "pv_estimate10": 1.5} for _ in range(48)]
        fake = _fake_self(
            {const.CONF_SOLAR_FORECAST_MODE: const.SOLAR_FORECAST_P50,
             const.CONF_SOLCAST_TODAY: SOLCAST_TODAY,
             const.CONF_SOLCAST_TOMORROW: SOLCAST_TOMORROW},
            {
                SOLCAST_TODAY: _FakeState({"detailedForecast": flat}),
                SCHEDULE_SENSOR: _FakeState({"schedule": _schedule(9000.0)}),
            },
        )
        forecast = fake._get_solcast_forecast("today")
        self.assertNotIn(SCHEDULE_SENSOR, fake.hass.states.looked_up)
        self.assertAlmostEqual(sum(forecast), const.SLOTS_PER_DAY * 3.0, places=6)

    def test_solcast_source_still_works_when_the_config_lists_no_emaldo(self):
        """Explicit source=solcast behaves exactly like the absent key."""
        flat = [{"pv_estimate": 2.0, "pv_estimate10": 2.0} for _ in range(48)]
        fake = _fake_self(
            {const.CONF_SOLAR_FORECAST_SOURCE: const.SOLAR_SOURCE_SOLCAST,
             const.CONF_SOLAR_FORECAST_MODE: const.SOLAR_FORECAST_P10,
             const.CONF_SOLCAST_TODAY: SOLCAST_TODAY,
             const.CONF_SOLCAST_TOMORROW: SOLCAST_TOMORROW},
            {SOLCAST_TODAY: _FakeState({"detailedForecast": flat})},
        )
        forecast = fake._get_solcast_forecast("today")
        self.assertAlmostEqual(sum(forecast), const.SLOTS_PER_DAY * 2.0, places=6)


class TestMissingEmaldoData(unittest.TestCase):
    def setUp(self):
        self.capture = _LogCapture()
        coord._LOGGER.addHandler(self.capture)
        self.addCleanup(coord._LOGGER.removeHandler, self.capture)

    def test_unresolvable_schedule_sensor_warns_once_and_plans_zero(self):
        fake = _fake_self(_emaldo_config(), {}, resolve=None)
        first = fake._get_solcast_forecast("today")
        second = fake._get_solcast_forecast("tomorrow")
        self.assertEqual(sum(first), 0.0)
        self.assertEqual(sum(second), 0.0)
        self.assertEqual(len(self.capture.records), 1)
        self.assertIn("schedule_chart", self.capture.records[0].getMessage())

    def test_all_zero_forecast_warns_once_not_every_run(self):
        """A Store with third-party PV off reports solar=0 — say it once."""
        fake = _fake_self(
            _emaldo_config(),
            {SCHEDULE_SENSOR: _FakeState({"schedule": _schedule(0.0)})},
        )
        for _ in range(4):
            fake._get_solcast_forecast("today")
        self.assertEqual(len(self.capture.records), 1)
        self.assertIn("zeros", self.capture.records[0].getMessage())

    def test_real_forecast_does_not_warn(self):
        fake = _fake_self(
            _emaldo_config(),
            {SCHEDULE_SENSOR: _FakeState({"schedule": _schedule(1200.0)})},
        )
        fake._get_solcast_forecast("today")
        self.assertEqual(self.capture.records, [])

    def test_sensor_without_schedule_attribute_plans_zero(self):
        fake = _fake_self(_emaldo_config(), {SCHEDULE_SENSOR: _FakeState({})})
        forecast = fake._get_solcast_forecast("today")
        self.assertEqual(len(forecast), const.SLOTS_PER_DAY)
        self.assertEqual(sum(forecast), 0.0)


class TestConfidenceRatioWithEmaldoSource(unittest.TestCase):
    def test_emaldo_source_reports_no_confidence_data_without_keyerror(self):
        """No p10/p50 pair exists in the Emaldo forecast.

        An emaldo-sourced entry never stores the Solcast sensor keys, so the
        lookup must not be attempted at all — a KeyError here would abort the
        coordinator update for every solar-aware grid charge.
        """
        fake = _fake_self(
            _emaldo_config(
                **{
                    const.CONF_SOLAR_FORECAST_MODE: const.SOLAR_FORECAST_P50,
                    const.CONF_GRID_CHARGE_SOLAR_AWARE: 0.5,
                }
            ),
            {SCHEDULE_SENSOR: _FakeState({"schedule": _schedule(2000.0)})},
        )
        self.assertIsNone(
            fake._get_solar_confidence_ratio("today")
        )
        self.assertEqual(fake.hass.states.looked_up, [])

    def test_solcast_source_still_computes_the_ratio(self):
        flat = [{"pv_estimate": 4.0, "pv_estimate10": 2.0} for _ in range(48)]
        fake = _fake_self(
            {
                const.CONF_SOLAR_FORECAST_MODE: const.SOLAR_FORECAST_P50,
                const.CONF_GRID_CHARGE_SOLAR_AWARE: 0.5,
                const.CONF_SOLCAST_TODAY: SOLCAST_TODAY,
                const.CONF_SOLCAST_TOMORROW: SOLCAST_TOMORROW,
            },
            {SOLCAST_TODAY: _FakeState({"detailedForecast": flat})},
        )
        self.assertAlmostEqual(
            fake._get_solar_confidence_ratio("today"),
            0.5,
            places=6,
        )


if __name__ == "__main__":
    unittest.main()
