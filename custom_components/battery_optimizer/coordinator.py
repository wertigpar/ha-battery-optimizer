"""Data coordinator for Battery Optimizer.

Gathers data from Nordpool, Solcast, and battery sensors, runs the optimizer,
and pushes the resulting schedule to the Emaldo integration.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.util import dt as dt_util

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    EMALDO_DOMAIN,
    SLOT_NO_OVERRIDE,
    SLOT_IDLE,
    SLOTS_PER_DAY,
    MIDNIGHT_CHECKPOINT,
    CONF_SPOT_SENSOR,
    CONF_SOLCAST_TODAY,
    CONF_SOLCAST_TOMORROW,
    CONF_VAT_MULTIPLIER,
    CONF_TRANSFER_FEE_BUY,
    CONF_SALES_COMMISSION,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_MAX_CHARGE_KW,
    CONF_MAX_DISCHARGE_KW,
    CONF_CHARGE_EFFICIENCY,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_SOC_MIN,
    CONF_SOC_MAX,
    CONF_BASE_LOAD_KW,
    CONF_BATTERY_WEAR_COST,
    CONF_IDLE_POWER_KW,
    CONF_ENABLE_SOC_SAFEGUARD,
    CONF_SOC_RECOVERY_BUFFER,
    DEFAULT_ENABLE_SOC_SAFEGUARD,
    DEFAULT_SOC_RECOVERY_BUFFER_PCT,
    LOW_SOC_RERUN_MARGIN_PCT,
    LOW_SOC_RERUN_THROTTLE_MIN,
    CONF_IDLE_STRATEGY,
    CONF_PRICE_SOURCE,
    PRICE_SOURCE_EMALDO,
    CONF_SOC_GUARD_INTERVAL,
    CONF_OPTIMIZER_INTERVAL,
    CONF_EMALDO_ENTRY_ID,
    CONF_AUTO_BASE_LOAD,
    CONF_LOAD_ENERGY_SENSOR,
    CONF_ENABLE_PV_STRATEGY,
    CONF_SOLAR_SELL_MIN_FORECAST_KWH,
    CONF_ENABLE_EMALDO_CONTROL,
    CONF_SOLAR_FORECAST_MODE,
    SOLAR_FORECAST_P10,
    DEFAULT_AUTO_BASE_LOAD,
    DEFAULT_LOAD_ENERGY_SENSOR,
    DEFAULT_ENABLE_PV_STRATEGY,
    DEFAULT_ENABLE_EMALDO_CONTROL,
    DEFAULT_SOLAR_SELL_MIN_FORECAST_KWH,
    DEFAULT_SOLAR_FORECAST_MODE,
    DEFAULT_BASE_LOAD_KW,
    DEFAULT_IDLE_STRATEGY,
    DEFAULT_SOC_GUARD_INTERVAL,
    DEFAULT_OPTIMIZER_INTERVAL,
    IDLE_FULL_CONTROL,
    IDLE_SOLAR_GUARD,
    IDLE_SMART_OVERRIDE,
)
from .optimizer import (
    BatteryConfig,
    OptimizationResult,
    compute_prices,
    interpolate_solar_to_15min,
    optimize,
)

_LOGGER = logging.getLogger(__name__)


def _current_slot_index() -> int:
    """Return the current 15-minute slot index (0-95)."""
    now = dt_util.now()
    return (now.hour * 60 + now.minute) // 15


def _action_to_mode(action: str) -> int:
    """Convert optimizer action string to numeric mode (1=charge, -1=discharge, 0=idle)."""
    if action in ("charge", "charge_floor"):
        return 1
    if action == "discharge":
        return -1
    return 0


class BatteryOptimizerCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that runs the battery optimizer on schedule.

    Triggers:
    - Nordpool sensor publishes tomorrow's prices (state change)
    - Fixed checkpoint times (00:01, 02:00, 06:00, 14:15, 18:00, 22:00)
    - Manual service call
    """

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,  # No automatic polling — event-driven
        )
        self._entry = entry
        self._unsub_listeners: list[CALLBACK_TYPE] = []
        self._last_result: OptimizationResult | None = None
        self._last_result_tomorrow: OptimizationResult | None = None
        self._last_run: datetime | None = None
        self._last_reason: str = ""
        self._activated_time: str | None = None
        # SoC Guard state
        self._unsub_guard: CALLBACK_TYPE | None = None
        self._current_guard_marker: int | None = None
        self._last_sent_slots: list[int] | None = None
        # Balancing state tracking
        self._balancing_sensor: str | None = None
        # Pinned Emaldo entry (from config, or auto-detected)
        self._emaldo_entry_id: str | None = self.config.get(CONF_EMALDO_ENTRY_ID)
        # Actual SoC history — recorded at each optimizer run for dashboard overlay
        self._actual_soc: list[dict] = []
        # Auto base load — cached result from last recorder query
        self._auto_base_load_value: float | None = None
        # Plan accuracy — planned vs actual energy for elapsed slots since last run
        self._last_run_slot: int | None = None
        self._last_run_initial_soc: float | None = None
        self._last_run_actual_snapshot: dict | None = None
        self._plan_accuracy: dict | None = None
        # PV sell strategy
        self._pv_strategy_enabled: bool = self.config.get(
            CONF_ENABLE_PV_STRATEGY, DEFAULT_ENABLE_PV_STRATEGY
        )
        self._unsub_pv_transitions: list[CALLBACK_TYPE] = []
        # Last known PV switch state — used for reconciliation
        self._pv_switch_state: bool | None = None
        # Emaldo control enable/disable
        self._emaldo_control_enabled: bool = self.config.get(
            CONF_ENABLE_EMALDO_CONTROL, DEFAULT_ENABLE_EMALDO_CONTROL
        )
        # Low-SoC forced re-run throttle
        self._last_low_soc_rerun: datetime | None = None
        # Startup flag — suppresses false-positive warnings before HA has fully started
        self._ha_started: bool = False

    @property
    def config(self) -> dict[str, Any]:
        """Merged config data + options."""
        return {**self._entry.data, **self._entry.options}

    @property
    def last_result(self) -> OptimizationResult | None:
        return self._last_result

    @property
    def last_result_tomorrow(self) -> OptimizationResult | None:
        return self._last_result_tomorrow

    @property
    def last_run(self) -> datetime | None:
        return self._last_run

    @property
    def last_reason(self) -> str:
        return self._last_reason

    @property
    def activated_time(self) -> str | None:
        return self._activated_time

    @property
    def actual_soc_history(self) -> list[dict]:
        """Actual SoC readings recorded at each optimizer run."""
        return self._actual_soc

    @property
    def auto_base_load_value(self) -> float | None:
        """Last computed auto base load (kW), or None before first run."""
        return self._auto_base_load_value

    @property
    def plan_accuracy(self) -> dict | None:
        """Plan vs actual accuracy dict from the last completed window."""
        return self._plan_accuracy

    @property
    def soc_guard_marker(self) -> int | None:
        """Current SoC guard high_marker, or None if guard is disabled."""
        guard_interval = self.config.get(
            CONF_SOC_GUARD_INTERVAL, DEFAULT_SOC_GUARD_INTERVAL
        )
        if guard_interval <= 0:
            return None
        return self._current_guard_marker

    def _find_balancing_sensor(self) -> str | None:
        """Return the balancing state entity_id, auto-discovered from the selected Emaldo entry."""
        return self._resolve_emaldo_entity("balancing_state")

    def _is_balancing_active(self) -> bool:
        """Return True when the Emaldo device is under grid-balancing control."""
        sensor_id = self._balancing_sensor
        if not sensor_id:
            return False
        state = self.hass.states.get(sensor_id)
        if state is None:
            return False
        return state.state not in ("idle", "unknown", "unavailable")

    @callback
    def _on_balancing_state_change(self, event) -> None:
        """Trigger a forced replan when balancing ends (any state → idle)."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if (
            old_state is not None
            and old_state.state not in ("idle", "unknown", "unavailable")
            and new_state is not None
            and new_state.state == "idle"
        ):
            _LOGGER.info(
                "Balancing ended (%s → idle) — scheduling immediate replan",
                old_state.state,
            )
            self.hass.async_create_task(
                self.run_optimizer(reason="balancing_ended", force=True)
            )

    @callback
    def _on_soc_state_change(self, event) -> None:
        """Trigger a forced replan when actual SoC nears the configured floor.

        The optimizer plan is built from a forecast — unexpected loads or a
        worse-than-forecast cloudy day can pull real SoC toward soc_min while
        the plan still shows healthy levels.  When SoC crosses
        soc_min + LOW_SOC_RERUN_MARGIN_PCT and no charge slot is imminent,
        force a re-run so the SoC safeguard can insert a keep-alive charge.
        """
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in ("unknown", "unavailable"):
            return
        try:
            soc = float(new_state.state)
        except (ValueError, TypeError):
            return

        cfg = self._build_battery_config()
        if not cfg.enable_soc_safeguard:
            return
        if soc >= cfg.soc_min + LOW_SOC_RERUN_MARGIN_PCT:
            return

        # Throttle — SoC updates arrive frequently near the threshold
        now = dt_util.now()
        if (
            self._last_low_soc_rerun is not None
            and (now - self._last_low_soc_rerun).total_seconds()
            < LOW_SOC_RERUN_THROTTLE_MIN * 60
        ):
            return

        # Skip if the current plan already charges within the next 2 hours
        if self._last_result is not None:
            now_slot = _current_slot_index()
            plan = {sp.index: sp for sp in self._last_result.slots}
            for s in range(now_slot, min(now_slot + 8, SLOTS_PER_DAY)):
                sp = plan.get(s)
                if sp is not None and sp.action in ("charge", "charge_floor"):
                    return

        self._last_low_soc_rerun = now
        _LOGGER.info(
            "Battery SoC %.1f%% below floor margin (%.0f%% + %.0f%%) with no "
            "imminent charge slot — forcing replan for keep-alive charge",
            soc, cfg.soc_min, LOW_SOC_RERUN_MARGIN_PCT,
        )
        self.hass.async_create_task(
            self.run_optimizer(reason="low_soc", force=True)
        )

    def _build_battery_config(self) -> BatteryConfig:
        """Create a BatteryConfig from the current HA config."""
        c = self.config
        # Use auto-tuned base load if enabled and a value has been computed;
        # otherwise fall back to the configured static value.
        base_load_kw = (
            self._auto_base_load_value
            if self._auto_base_load_value is not None
            and c.get(CONF_AUTO_BASE_LOAD, DEFAULT_AUTO_BASE_LOAD)
            else c.get(CONF_BASE_LOAD_KW, 0.5)
        )
        return BatteryConfig(
            capacity_kwh=c.get(CONF_BATTERY_CAPACITY_KWH, 5.0),
            max_charge_kw=c.get(CONF_MAX_CHARGE_KW, 2.5),
            max_discharge_kw=c.get(CONF_MAX_DISCHARGE_KW, 2.5),
            charge_efficiency=c.get(CONF_CHARGE_EFFICIENCY, 0.95),
            discharge_efficiency=c.get(CONF_DISCHARGE_EFFICIENCY, 0.95),
            soc_min=c.get(CONF_SOC_MIN, 20),
            soc_max=c.get(CONF_SOC_MAX, 100),
            vat_multiplier=c.get(CONF_VAT_MULTIPLIER, 1.255),
            transfer_fee_buy=c.get(CONF_TRANSFER_FEE_BUY, 0.0572),
            sales_commission=c.get(CONF_SALES_COMMISSION, 0.002),
            base_load_kw=base_load_kw,
            wear_cost_per_kwh=c.get(CONF_BATTERY_WEAR_COST, 0.03),
            idle_power_kw=c.get(CONF_IDLE_POWER_KW, 0.1),
            enable_soc_safeguard=c.get(
                CONF_ENABLE_SOC_SAFEGUARD, DEFAULT_ENABLE_SOC_SAFEGUARD
            ),
            soc_recovery_buffer_pct=c.get(
                CONF_SOC_RECOVERY_BUFFER, DEFAULT_SOC_RECOVERY_BUFFER_PCT
            ),
        )

    # ── Data readers ──────────────────────────────────────────────────

    def _parse_price_data(
        self,
    ) -> tuple[list[float] | None, list[float] | None]:
        """Return today/tomorrow 96-slot spot prices in €/kWh.

        Dispatches to the Emaldo internal source or the external sensor
        depending on the CONF_PRICE_SOURCE setting.
        """
        if self.config.get(CONF_PRICE_SOURCE, PRICE_SOURCE_EMALDO) == PRICE_SOURCE_EMALDO:
            return self._parse_emaldo_price_data()
        return self._parse_sensor_price_data()

    def _parse_emaldo_price_data(
        self,
    ) -> tuple[list[float] | None, list[float] | None]:
        """Read prices from sensor.power_store_schedule_chart (Emaldo internal).

        The schedule attribute contains a flat list of 192 × 15-min dicts:
          {"t": "2026-05-06T00:00:00+03:00", "price": 5.0, ...}
        Prices are raw Nord Pool spot in ct/kWh (snt/kWh).  Converted to
        €/kWh by dividing by 100.
        Slots are grouped by local date from the "t" field — the schedule is a
        rolling 48-hour window and the first slot is not necessarily today midnight.
        Returns None for tomorrow if its 96 slots are missing or all zero.
        """
        # Resolve the Emaldo schedule_chart entity dynamically
        sensor_id = self._resolve_emaldo_entity("schedule_chart")
        if not sensor_id:
            _LOGGER.warning(
                "Emaldo schedule_chart sensor not found — falling back to external price sensor"
            )
            return self._parse_sensor_price_data()

        state = self.hass.states.get(sensor_id)
        if state is None:
            _LOGGER.warning("Emaldo schedule_chart sensor %s unavailable", sensor_id)
            return self._parse_sensor_price_data()

        schedule = state.attributes.get("schedule")
        if not schedule or not isinstance(schedule, list) or len(schedule) < SLOTS_PER_DAY:
            _LOGGER.warning("Emaldo schedule_chart has no usable schedule attribute")
            return self._parse_sensor_price_data()

        # Group slots by local date using the "t" timestamp in each slot.
        # The schedule is a rolling window — the first slot is NOT necessarily
        # midnight of today, so we must not blindly split at index 96.
        local_tz = dt_util.get_time_zone(self.hass.config.time_zone)
        today_date = dt_util.now().date()
        tomorrow_date = today_date + timedelta(days=1)

        today_slots: list[dict] = []
        tomorrow_slots: list[dict] = []
        for slot in schedule:
            try:
                slot_dt = datetime.fromisoformat(slot["t"])
                slot_date = slot_dt.astimezone(local_tz).date()
            except (KeyError, ValueError, TypeError):
                continue
            if slot_date == today_date:
                today_slots.append(slot)
            elif slot_date == tomorrow_date:
                tomorrow_slots.append(slot)

        if len(today_slots) < SLOTS_PER_DAY:
            _LOGGER.warning(
                "Emaldo schedule_chart has only %d slots for today (expected %d)",
                len(today_slots),
                SLOTS_PER_DAY,
            )
            return self._parse_sensor_price_data()

        today_prices = [s["price"] / 100.0 for s in today_slots[:SLOTS_PER_DAY]]

        if len(tomorrow_slots) == SLOTS_PER_DAY and any(s["price"] != 0 for s in tomorrow_slots):
            tomorrow_prices: list[float] | None = [s["price"] / 100.0 for s in tomorrow_slots]
        else:
            tomorrow_prices = None

        return today_prices, tomorrow_prices

    def _parse_sensor_price_data(
        self,
    ) -> tuple[list[float] | None, list[float] | None]:
        """Parse the external Nordpool sensor 'data' attribute into today/tomorrow 96-slot prices.

        The sensor stores a flat list of {start, end, price} dicts at 15-min
        resolution, with prices in snt/kWh.  We split by date, convert to
        €/kWh, and return (today_96, tomorrow_96).  Either may be None if
        insufficient data exists.
        """
        sensor_id = self.config.get(CONF_SPOT_SENSOR, "")
        if not sensor_id:
            _LOGGER.warning("Price source is 'sensor' but no sensor is configured")
            return None, None
        state = self.hass.states.get(sensor_id)
        if state is None:
            _LOGGER.warning("Price sensor %s not found", sensor_id)
            return None, None
        if state.state in ("unavailable", "unknown"):
            return None, None

        data = state.attributes.get("data")
        if not data or not isinstance(data, list):
            _LOGGER.warning("Price sensor %s has no 'data' attribute", sensor_id)
            return None, None

        # Detect unit — convert snt/kWh (cents) → €/kWh
        unit = str(state.attributes.get("unit_of_measurement", "")).lower()
        is_cents = "snt" in unit or "cent" in unit or "c/kwh" in unit

        today_date = dt_util.now().date()
        tomorrow_date = today_date + timedelta(days=1)

        today_prices: list[float | None] = [None] * SLOTS_PER_DAY
        tomorrow_prices: list[float | None] = [None] * SLOTS_PER_DAY

        for entry in data:
            start_str = entry.get("start")
            price = entry.get("price")
            if start_str is None or price is None:
                continue
            try:
                start_dt = datetime.fromisoformat(str(start_str))
            except (ValueError, TypeError):
                continue

            slot_idx = (start_dt.hour * 60 + start_dt.minute) // 15
            if not 0 <= slot_idx < SLOTS_PER_DAY:
                continue

            price_eur = float(price) / 100.0 if is_cents else float(price)
            entry_date = start_dt.date()

            if entry_date == today_date:
                today_prices[slot_idx] = price_eur
            elif entry_date == tomorrow_date:
                tomorrow_prices[slot_idx] = price_eur

        today_result = self._fill_price_gaps(today_prices)
        tomorrow_result = self._fill_price_gaps(tomorrow_prices)

        return today_result, tomorrow_result

    @staticmethod
    def _fill_price_gaps(prices: list[float | None]) -> list[float] | None:
        """Fill None gaps in a 96-slot price list using nearest neighbour.

        Returns None if fewer than 10 slots were populated.
        """
        filled_count = sum(1 for p in prices if p is not None)
        if filled_count < 10:
            return None

        result = list(prices)
        # Forward fill
        last_val: float | None = None
        for i in range(len(result)):
            if result[i] is not None:
                last_val = result[i]
            elif last_val is not None:
                result[i] = last_val
        # Backward fill any leading Nones
        first_val = next((v for v in result if v is not None), 0.0)
        for i in range(len(result)):
            if result[i] is None:
                result[i] = first_val
            else:
                break

        return result  # type: ignore[return-value]

    def _has_tomorrow_prices(self) -> bool:
        """Check if tomorrow's prices are available."""
        if self.config.get(CONF_PRICE_SOURCE, PRICE_SOURCE_EMALDO) == PRICE_SOURCE_EMALDO:
            _, tomorrow = self._parse_emaldo_price_data()
            return tomorrow is not None
        # External sensor path
        sensor_id = self.config.get(CONF_SPOT_SENSOR, "")
        if not sensor_id:
            return False
        state = self.hass.states.get(sensor_id)
        if state is None:
            return False
        tomorrow_valid = state.attributes.get("tomorrow_valid")
        if tomorrow_valid is True:
            return True
        if tomorrow_valid is False:
            return False  # sensor explicitly reports no tomorrow prices — skip parse
        # tomorrow_valid key absent — fall back to parsing data array
        _, tomorrow = self._parse_sensor_price_data()
        return tomorrow is not None

    def _get_solcast_forecast(self, which: str = "today") -> list[float]:
        """Read Solcast forecast from HA sensor attributes.

        Args:
            which: "today" or "tomorrow".

        Returns:
            96 x 15-min kW values.
        """
        if which == "today":
            sensor_id = self.config[CONF_SOLCAST_TODAY]
        else:
            sensor_id = self.config[CONF_SOLCAST_TOMORROW]

        state = self.hass.states.get(sensor_id)
        if state is None:
            _LOGGER.debug("Solcast sensor %s not found", sensor_id)
            return [0.0] * SLOTS_PER_DAY

        detailed = state.attributes.get("detailedForecast")
        if not detailed or not isinstance(detailed, list):
            _LOGGER.debug("Solcast sensor %s has no detailedForecast", sensor_id)
            return [0.0] * SLOTS_PER_DAY

        forecast_mode = self.config.get(CONF_SOLAR_FORECAST_MODE, DEFAULT_SOLAR_FORECAST_MODE)
        if forecast_mode == SOLAR_FORECAST_P10:
            _LOGGER.debug("Solar forecast mode: P10 (pessimistic/weather-aware)")
            slots_30min = [
                s.get("pv_estimate10", s.get("pv_estimate", 0.0)) for s in detailed
            ]
        else:
            _LOGGER.debug("Solar forecast mode: P50 (median)")
            slots_30min = [s.get("pv_estimate", 0.0) for s in detailed]
        return interpolate_solar_to_15min(slots_30min)

    def _resolve_emaldo_entity(self, key: str, domain: str = "sensor") -> str | None:
        """Resolve an Emaldo entity_id from the entity registry.

        Uses the emaldo coordinator's home_id to construct the unique_id
        pattern ``{home_id}_{key}``.  This works for any Emaldo device model
        (Power Store, Power Core, …) regardless of the slugified device name.

        Args:
            key: The key suffix as used in the Emaldo unique_id (e.g. ``battery_soc``).
            domain: HA entity domain to look in (default ``"sensor"``; use
                    ``"switch"`` for switch entities).

        Returns:
            entity_id string, or None if not found.
        """
        emaldo_data = self.hass.data.get(EMALDO_DOMAIN)
        if not emaldo_data:
            return None
        # Use pinned entry if available, otherwise iterate
        entries = (
            [(self._emaldo_entry_id, emaldo_data[self._emaldo_entry_id])]
            if self._emaldo_entry_id and self._emaldo_entry_id in emaldo_data
            else emaldo_data.items()
        )
        for entry_id, entry_data in entries:
            coord = entry_data.get("power")  # EmaldoCoordinator — holds home_id
            if coord is None:
                continue
            home_id = getattr(coord, "home_id", None)
            if not home_id:
                continue
            unique_id = f"{home_id}_{key}"
            registry = er.async_get(self.hass)
            entity_id = registry.async_get_entity_id(domain, EMALDO_DOMAIN, unique_id)
            if entity_id:
                return entity_id
        return None

    def _get_battery_soc(self) -> float | None:
        """Read current battery SoC, auto-discovered from the selected Emaldo entry."""
        sensor_id = self._resolve_emaldo_entity("battery_soc")
        if not sensor_id:
            if self._ha_started:
                _LOGGER.warning(
                    "Battery SoC sensor could not be auto-discovered from the Emaldo integration"
                )
            return None
        state = self.hass.states.get(sensor_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    def _read_emaldo_sensor_float(self, key: str) -> float | None:
        """Read a float value from an Emaldo sensor by emaldo key."""
        sensor_id = self._resolve_emaldo_entity(key)
        if not sensor_id:
            return None
        state = self.hass.states.get(sensor_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    # ── Auto base load ────────────────────────────────────────────────

    async def _fetch_auto_base_load_kw(self) -> float:
        """Return auto-tuned base load kW from 14-day HA recorder statistics.

        Queries the daily ``mean`` of the configured household load power sensor
        (W, ``state_class: measurement``) over the last 14 days, converts to kW,
        and returns the 7-day rolling average clamped to ±50 % of the configured
        ``base_load_kw``.

        Configure with a combined household load power sensor in Watts, e.g.
        ``sensor.emhass_combined_power``.

        Falls back to the configured ``base_load_kw`` if:
        - auto-tune is disabled,
        - no sensor is configured,
        - the recorder component is unavailable, or
        - fewer than 3 days of data exist.
        """
        configured = self.config.get(CONF_BASE_LOAD_KW, DEFAULT_BASE_LOAD_KW)
        if not self.config.get(CONF_AUTO_BASE_LOAD, DEFAULT_AUTO_BASE_LOAD):
            return configured

        load_sensor = self.config.get(CONF_LOAD_ENERGY_SENSOR, DEFAULT_LOAD_ENERGY_SENSOR)
        if not load_sensor:
            _LOGGER.debug("Auto base load: no sensor configured, using %.2f kW", configured)
            return configured

        try:
            from homeassistant.components.recorder import get_instance  # noqa: PLC0415
            from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
                statistics_during_period,
            )
        except ImportError:
            _LOGGER.debug("Recorder not available — using configured base_load_kw")
            return configured

        now = dt_util.now()
        start = now - timedelta(days=14)
        try:
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start,
                None,
                {load_sensor},
                "day",
                None,
                {"mean"},
            )
        except Exception:
            _LOGGER.debug(
                "Recorder query failed — using configured base_load_kw", exc_info=True
            )
            return configured

        rows = stats.get(load_sensor, [])
        # mean is in W; convert to kW and keep only positive (load) values
        daily_kw = [row["mean"] / 1000.0 for row in rows if row.get("mean") is not None and row["mean"] > 0]

        if len(daily_kw) < 3:
            _LOGGER.debug(
                "Auto base load: only %d days of data (need ≥3) — using %.2f kW",
                len(daily_kw),
                configured,
            )
            return configured

        window = daily_kw[-7:]
        avg_kw = sum(window) / len(window)
        lo = configured * 0.5
        hi = configured * 2.0
        result = max(lo, min(hi, avg_kw))
        _LOGGER.info(
            "Auto base load: %d-day window, daily avg=%.3f kW "
            "(configured=%.3f kW, clamped [%.3f, %.3f]) → %.3f kW",
            len(window),
            avg_kw,
            configured,
            lo,
            hi,
            result,
        )
        return result

    # ── Plan accuracy ─────────────────────────────────────────────────

    def _compute_plan_accuracy(self, now_slot: int) -> dict | None:
        """Compare planned vs actual energy for slots elapsed since the last run.

        Sums planned charge/discharge/solar kWh from the previous optimizer
        result for the elapsed slots and compares with the delta of actual
        Emaldo sensor readings since the last run snapshot.

        Returns None if insufficient data (no prior result or no elapsed slots).
        """
        if (
            self._last_result is None
            or self._last_run_slot is None
            or self._last_run_initial_soc is None
            or now_slot <= self._last_run_slot
        ):
            return None

        cfg = self._build_battery_config()
        capacity = cfg.capacity_kwh

        slot_plan = {sp.index: sp for sp in self._last_result.slots}
        planned_discharge = 0.0
        planned_charge = 0.0
        planned_solar = 0.0
        elapsed = 0
        prev_soc: float | None = self._last_run_initial_soc

        for s in range(self._last_run_slot, now_slot):
            sp = slot_plan.get(s)
            if sp is None:
                prev_soc = None
                elapsed += 1
                continue
            if prev_soc is not None:
                soc_delta = sp.soc_after - prev_soc
                kwh = abs(soc_delta) * capacity / 100.0
                if sp.action in ("charge", "charge_floor"):
                    planned_charge += kwh
                elif sp.action == "discharge":
                    planned_discharge += kwh
            planned_solar += sp.solar_kw * 0.25
            prev_soc = sp.soc_after
            elapsed += 1

        snap = self._last_run_actual_snapshot or {}
        accuracy: dict = {
            "elapsed_slots": elapsed,
            "planned_discharge_kwh": round(planned_discharge, 3),
            "planned_charge_kwh": round(planned_charge, 3),
            "planned_solar_kwh": round(planned_solar, 3),
            "last_run": self._last_run.isoformat() if self._last_run else None,
        }

        for emaldo_key, snap_key in [
            ("battery_discharged_today", "discharge"),
            ("battery_charged_today", "charge"),
            ("solar_energy_today", "solar"),
        ]:
            actual = self._read_emaldo_sensor_float(emaldo_key)
            if actual is not None and snap_key in snap and snap[snap_key] is not None:
                delta = actual - snap[snap_key]
                # Guard against midnight sensor reset (values drop back to 0)
                if delta >= -0.1:
                    actual_kwh = round(max(delta, 0.0), 3)
                    accuracy[f"actual_{snap_key}_kwh"] = actual_kwh
                    accuracy[f"{snap_key}_error_kwh"] = round(
                        actual_kwh - accuracy[f"planned_{snap_key}_kwh"], 3
                    )

        return accuracy

    # ── Optimizer entry point ─────────────────────────────────────────

    async def run_optimizer(
        self, reason: str = "manual", force: bool = True
    ) -> OptimizationResult | None:
        """Run the optimizer and push the schedule to Emaldo.

        Args:
            reason: Why this run was triggered.
            force: If False, skip if conditions haven't changed enough.
        """
        _LOGGER.info("Optimizer triggered: reason=%s, force=%s", reason, force)

        # Gather data
        try:
            prices_today, prices_tomorrow = self._parse_price_data()
        except Exception:
            _LOGGER.exception("Failed to parse price data")
            return None

        if prices_today is None:
            _LOGGER.error("Cannot optimize: no prices available")
            return None

        _LOGGER.info(
            "Prices parsed: %d slots, range %.4f–%.4f €/kWh",
            len(prices_today), min(prices_today), max(prices_today),
        )

        solar = self._get_solcast_forecast("today")
        soc = self._get_battery_soc()
        self._auto_base_load_value = await self._fetch_auto_base_load_kw()
        cfg = self._build_battery_config()
        now_slot = _current_slot_index()

        if soc is None:
            if self._ha_started:
                _LOGGER.error(
                    "Cannot optimize: battery SoC could not be read. "
                    "Ensure the Emaldo integration is loaded and the device is online.",
                )
            else:
                _LOGGER.debug(
                    "Skipping startup optimizer run — Emaldo integration not ready yet"
                )
            return None

        _LOGGER.info("Battery SoC: %.1f%%, start_slot: %d", soc, now_slot)

        # Record actual SoC for dashboard overlay
        self._actual_soc.append({"t": dt_util.now().isoformat(), "soc": round(soc, 1)})
        if len(self._actual_soc) > 192:  # cap at ~2 days of 15-min readings
            self._actual_soc = self._actual_soc[-192:]

        if not force and self._last_result is not None:
            if not self._should_reoptimize(soc, cfg):
                _LOGGER.info("Skipping optimization — no significant changes")
                return self._last_result

        # Compute plan accuracy from the previous result before overwriting
        new_accuracy = self._compute_plan_accuracy(now_slot)
        if new_accuracy is not None:
            self._plan_accuracy = new_accuracy
            _LOGGER.info(
                "Plan accuracy (%d slots): "
                "discharge planned=%.3f actual=%s kWh, "
                "charge planned=%.3f actual=%s kWh, "
                "solar planned=%.3f actual=%s kWh",
                new_accuracy["elapsed_slots"],
                new_accuracy["planned_discharge_kwh"],
                new_accuracy.get("actual_discharge_kwh", "N/A"),
                new_accuracy["planned_charge_kwh"],
                new_accuracy.get("actual_charge_kwh", "N/A"),
                new_accuracy["planned_solar_kwh"],
                new_accuracy.get("actual_solar_kwh", "N/A"),
            )

        # Run optimizer — prices_today is already 96 x 15-min in €/kWh
        buy_prices, sell_prices = compute_prices(prices_today, cfg)
        result = optimize(
            buy_prices,
            sell_prices,
            solar,
            cfg,
            start_slot=now_slot,
            initial_soc_pct=soc,
            enable_pv_strategy=self._pv_strategy_enabled,
        )
        result.reason = reason

        self._last_result = result
        self._last_run = dt_util.now()
        self._last_reason = reason

        # Snapshot actual values for next accuracy comparison
        self._last_run_slot = now_slot
        self._last_run_initial_soc = soc
        self._last_run_actual_snapshot = {
            "discharge": self._read_emaldo_sensor_float("battery_discharged_today"),
            "charge": self._read_emaldo_sensor_float("battery_charged_today"),
            "solar": self._read_emaldo_sensor_float("solar_energy_today"),
        }

        # Optimize tomorrow if prices available
        if prices_tomorrow is not None:
            solar_tomorrow = self._get_solcast_forecast("tomorrow")
            end_soc = result.slots[-1].soc_after if result.slots else None
            buy_tom, sell_tom = compute_prices(prices_tomorrow, cfg)
            result_tomorrow = optimize(
                buy_tom,
                sell_tom,
                solar_tomorrow,
                cfg,
                start_slot=0,
                initial_soc_pct=end_soc,
                enable_pv_strategy=self._pv_strategy_enabled,
            )
            self._last_result_tomorrow = result_tomorrow
            _LOGGER.info(
                "Tomorrow optimization: profit=%.4f€, C=%d D=%d I=%d",
                result_tomorrow.total_profit,
                result_tomorrow.charge_slots,
                result_tomorrow.discharge_slots,
                result_tomorrow.idle_slots,
            )
        else:
            self._last_result_tomorrow = None

        # Push today (+ tomorrow if available) to Emaldo
        await self._push_schedule(result, self._last_result_tomorrow)

        # Apply PV sell strategy (controls Emaldo third-party PV switch)
        await self._apply_pv_strategy(result)

        # Compute activated time window
        self._compute_activated_time(result, self._last_result_tomorrow)

        # Update HA state
        self.async_set_updated_data({
            "result": result,
            "result_tomorrow": self._last_result_tomorrow,
            "last_run": self._last_run.isoformat(),
            "reason": reason,
            "activated_time": self._activated_time,
        })

        return result

    def _should_reoptimize(self, current_soc: float | None, cfg: BatteryConfig) -> bool:
        """Check if conditions changed enough to warrant re-optimization.

        Returns True if SoC deviation > 10% from planned, or if no previous
        result exists.
        """
        if self._last_result is None or current_soc is None:
            return True

        now_slot = _current_slot_index()
        if now_slot >= len(self._last_result.slots):
            return True

        planned_soc = self._last_result.slots[now_slot].soc_after
        deviation = abs(current_soc - planned_soc)
        if deviation > 10.0:
            _LOGGER.info(
                "SoC deviation: actual=%.1f%%, planned=%.1f%% — re-optimizing",
                current_soc, planned_soc,
            )
            return True

        return False

    async def _push_schedule(
        self,
        result: OptimizationResult,
        result_tomorrow: OptimizationResult | None = None,
    ) -> None:
        """Push optimizer schedule to Emaldo using rolling 24h slot mapping.

        The Emaldo E2E override uses a rolling 24-hour window:
        - E2E slots [now_slot..95] → today's remaining slots
        - E2E slots [0..now_slot-1] → tomorrow's early slots

        This allows a single 96-slot push to cover the rest of today plus
        the beginning of tomorrow (up to the current time-of-day).

        Uses smart diffing: compares the optimizer plan against the battery's
        internal AI schedule and only overrides slots that differ.
        """
        if not self._emaldo_control_enabled:
            _LOGGER.debug(
                "Emaldo control disabled — schedule computed but not applied"
            )
            return

        if not self.hass.services.has_service(EMALDO_DOMAIN, "apply_bulk_schedule"):
            _LOGGER.warning(
                "Emaldo service 'apply_bulk_schedule' not available — "
                "schedule computed but not applied"
            )
            return

        if self._is_balancing_active():
            _LOGGER.info(
                "Balancing active (%s) — skipping schedule push",
                self.hass.states.get(self._balancing_sensor).state,
            )
            return

        emaldo_modes = self._read_emaldo_internal_modes()
        now_slot = _current_slot_index()

        idle_strategy = self.config.get(CONF_IDLE_STRATEGY, DEFAULT_IDLE_STRATEGY)

        # Pre-compute solar data for idle strategies that need it
        solar_today: list[float] | None = None
        solar_tomorrow: list[float] | None = None
        if idle_strategy in (IDLE_SOLAR_GUARD, IDLE_SMART_OVERRIDE):
            solar_today = self._get_solcast_forecast("today")
            solar_tomorrow = self._get_solcast_forecast("tomorrow")

        # Build rolling 96-slot array
        slot_values: list[int] = [SLOT_NO_OVERRIDE] * SLOTS_PER_DAY
        overrides_needed = 0

        # --- Today's remaining slots: E2E positions [now_slot..95] ---
        today_plan = {sp.index: sp for sp in result.slots}
        for e2e_pos in range(now_slot, SLOTS_PER_DAY):
            sp = today_plan.get(e2e_pos)
            if sp is None or sp.action == "none":
                # Apply idle strategy instead of leaving SLOT_NO_OVERRIDE
                if self._should_force_idle(
                    idle_strategy, e2e_pos, solar_today,
                    emaldo_modes, e2e_pos,
                ):
                    slot_values[e2e_pos] = SLOT_IDLE
                    overrides_needed += 1
                continue
            # Decide whether to defer this slot to the battery's internal
            # AI schedule (i.e. skip the override).  The internal schedule
            # is volatile: the AI recomputes it continuously, so a slot
            # that "matches" our plan at read time can silently revert to a
            # different mode minutes later — leaving our plan unenforced and
            # the active schedule diverging from the optimization plan.
            # Therefore active actions (charge / charge_floor / discharge)
            # and idle in full_control mode are ALWAYS enforced.  The
            # smart-diff (defer to the AI when modes already agree) is
            # applied only to idle slots under the AI-cooperative idle
            # strategies, where ceding quiet slots to the AI is intended.
            if sp.action == "idle" and idle_strategy != IDLE_FULL_CONTROL:
                if emaldo_modes is not None and e2e_pos < len(emaldo_modes):
                    if _action_to_mode(sp.action) == emaldo_modes[e2e_pos]:
                        continue
            slot_values[e2e_pos] = sp.slot_value
            overrides_needed += 1

        # --- Tomorrow's early slots: E2E positions [0..now_slot-1] ---
        if result_tomorrow is not None and now_slot > 0:
            tomorrow_plan = {sp.index: sp for sp in result_tomorrow.slots}
            for e2e_pos in range(0, now_slot):
                sp = tomorrow_plan.get(e2e_pos)
                if sp is None or sp.action == "none":
                    # Apply idle strategy for tomorrow's slots
                    if self._should_force_idle(
                        idle_strategy, e2e_pos, solar_tomorrow,
                        emaldo_modes, SLOTS_PER_DAY + e2e_pos,
                    ):
                        slot_values[e2e_pos] = SLOT_IDLE
                        overrides_needed += 1
                    continue
                # Same enforcement rule as today's loop: only idle slots
                # under AI-cooperative idle strategies are deferred to the
                # internal schedule; charge/discharge are always enforced.
                if sp.action == "idle" and idle_strategy != IDLE_FULL_CONTROL:
                    if (
                        emaldo_modes is not None
                        and (SLOTS_PER_DAY + e2e_pos) < len(emaldo_modes)
                    ):
                        if _action_to_mode(sp.action) == emaldo_modes[SLOTS_PER_DAY + e2e_pos]:
                            continue
                slot_values[e2e_pos] = sp.slot_value
                overrides_needed += 1

        if overrides_needed == 0:
            _LOGGER.info(
                "Optimizer plan matches battery internal schedule — "
                "no overrides needed"
            )
            await self._refresh_emaldo_schedule()
            return

        # SoC Guard: remap discharge slot values to use a unified
        # high_marker and send it as a global parameter.  The Emaldo
        # firmware uses the Battery Range (high/low markers) globally —
        # per-slot discharge thresholds are not independently honoured.
        guard_interval = self.config.get(
            CONF_SOC_GUARD_INTERVAL, DEFAULT_SOC_GUARD_INTERVAL
        )
        soc_guard_enabled = guard_interval > 0
        service_data: dict[str, Any] = {"slots": slot_values}

        if soc_guard_enabled:
            cfg = self._build_battery_config()
            high_marker = self._compute_soc_guard_marker()
            # Remap all discharge bytes to match the guard marker
            for i, val in enumerate(slot_values):
                if val > 0x80:  # discharge byte (129-255)
                    slot_values[i] = (256 - high_marker) & 0xFF
            service_data["high_marker"] = high_marker
            service_data["low_marker"] = int(cfg.soc_min)
            self._current_guard_marker = high_marker
            _LOGGER.info(
                "SoC guard active: high_marker=%d%%, low_marker=%d%%",
                high_marker, int(cfg.soc_min),
            )

        self._last_sent_slots = list(slot_values)

        _LOGGER.info(
            "Pushing rolling 96-slot schedule to Emaldo: %d overrides "
            "(now_slot=%d, idle_strategy=%s, today remaining=%d, tomorrow wrapped=%d)",
            overrides_needed, now_slot, idle_strategy,
            SLOTS_PER_DAY - now_slot, now_slot,
        )

        try:
            await self.hass.services.async_call(
                EMALDO_DOMAIN,
                "apply_bulk_schedule",
                service_data,
                blocking=True,
            )
            _LOGGER.info("Rolling schedule applied to Emaldo successfully")
        except Exception as err:
            _LOGGER.error("Failed to push schedule to Emaldo: %s", err)

    def _compute_activated_time(
        self,
        result: OptimizationResult,
        result_tomorrow: OptimizationResult | None = None,
    ) -> None:
        """Compute the time window that has been sent to the battery as overrides.

        With the rolling 24h slot model, the pushed window covers:
        - Today from now_slot to end of day
        - Tomorrow from midnight to now_slot (if tomorrow result available)
        """
        now_slot = _current_slot_index()

        active_today = [
            sp for sp in result.slots
            if sp.index >= now_slot and sp.action != "none"
        ]
        active_tomorrow = []
        if result_tomorrow is not None and now_slot > 0:
            active_tomorrow = [
                sp for sp in result_tomorrow.slots
                if sp.index < now_slot and sp.action != "none"
            ]

        if not active_today and not active_tomorrow:
            self._activated_time = None
            return

        # Build time string showing the rolling coverage
        parts = []
        if active_today:
            first = active_today[0]
            last = active_today[-1]
            h1, m1 = (first.index * 15) // 60, (first.index * 15) % 60
            h2, m2 = (last.index * 15) // 60, (last.index * 15) % 60
            parts.append(f"Today {h1:02d}:{m1:02d}–{h2:02d}:{m2:02d}")
        if active_tomorrow:
            first = active_tomorrow[0]
            last = active_tomorrow[-1]
            h1, m1 = (first.index * 15) // 60, (first.index * 15) % 60
            h2, m2 = (last.index * 15) // 60, (last.index * 15) % 60
            parts.append(f"Tomorrow {h1:02d}:{m1:02d}–{h2:02d}:{m2:02d}")

        self._activated_time = " + ".join(parts)

    @staticmethod
    def _first_solar_slot(solar_forecast: list[float] | None) -> int | None:
        """Return the first slot index with significant solar (> 0.1 kW)."""
        if not solar_forecast:
            return None
        for i, val in enumerate(solar_forecast):
            if val > 0.1:
                return i
        return None

    def _should_force_idle(
        self,
        strategy: str,
        slot_index: int,
        solar_forecast: list[float] | None,
        emaldo_modes: list[int] | None,
        emaldo_index: int,
    ) -> bool:
        """Decide whether an idle slot should be forced to SLOT_IDLE.

        Args:
            strategy: Idle strategy from config.
            slot_index: Time-of-day slot index (0-95).
            solar_forecast: 96 × 15-min solar kW for that day, or None.
            emaldo_modes: Internal AI schedule modes, or None.
            emaldo_index: Index into emaldo_modes for this slot.
        """
        if strategy == IDLE_FULL_CONTROL:
            return True

        first_solar = self._first_solar_slot(solar_forecast)

        if strategy == IDLE_SOLAR_GUARD:
            # Force idle for slots before solar production starts
            return first_solar is not None and slot_index < first_solar

        if strategy == IDLE_SMART_OVERRIDE:
            # Force idle when internal AI plans to charge AND solar is coming
            if first_solar is None or slot_index >= first_solar:
                return False
            if emaldo_modes is not None and emaldo_index < len(emaldo_modes):
                return emaldo_modes[emaldo_index] == 1  # AI plans charge
            return False

        return False

    def _read_emaldo_internal_modes(self) -> list[int] | None:
        """Read the battery's internal AI schedule modes from the Emaldo integration.

        Returns a list of mode values (1=charge, -1=discharge, 0=idle)
        for all available slots (96 or 192), or None if unavailable.
        """
        emaldo_data = self.hass.data.get(EMALDO_DOMAIN)
        if not emaldo_data:
            _LOGGER.debug("Emaldo integration data not available for smart diff")
            return None

        entries = (
            [(self._emaldo_entry_id, emaldo_data[self._emaldo_entry_id])]
            if self._emaldo_entry_id and self._emaldo_entry_id in emaldo_data
            else emaldo_data.items()
        )
        for entry_id, entry_data in entries:
            sched_coord = entry_data.get("schedule")
            if sched_coord is None or sched_coord.data is None:
                continue
            schedule = sched_coord.data.get("schedule") or {}
            slots = schedule.get("hope_charge_discharges", [])
            if not slots:
                continue
            modes: list[int] = []
            for v in slots:
                if v == 100:
                    modes.append(1)   # charge
                elif v < 0:
                    modes.append(-1)  # discharge
                else:
                    modes.append(0)   # idle
            return modes

        _LOGGER.debug("No Emaldo schedule data found for smart diff")
        return None

    async def _refresh_emaldo_schedule(self) -> None:
        """Trigger a refresh of the Emaldo schedule coordinator."""
        emaldo_data = self.hass.data.get(EMALDO_DOMAIN)
        if not emaldo_data:
            return
        entries = (
            [(self._emaldo_entry_id, emaldo_data[self._emaldo_entry_id])]
            if self._emaldo_entry_id and self._emaldo_entry_id in emaldo_data
            else emaldo_data.items()
        )
        for entry_id, entry_data in entries:
            sched_coord = entry_data.get("schedule")
            if sched_coord is not None:
                await sched_coord.async_request_refresh()
                return

    # ── Listeners ─────────────────────────────────────────────────────

    @callback
    def async_setup_listeners(self) -> None:
        """Set up time-based and event-based triggers."""
        # Cancel any existing listeners
        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()

        # 1) Fixed midnight checkpoint (always runs at 00:01)
        hour, minute = MIDNIGHT_CHECKPOINT
        unsub = async_track_time_change(
            self.hass,
            self._checkpoint_callback,
            hour=hour,
            minute=minute,
            second=0,
        )
        self._unsub_listeners.append(unsub)

        # 2) Configurable periodic optimizer re-run
        opt_interval = self.config.get(
            CONF_OPTIMIZER_INTERVAL, DEFAULT_OPTIMIZER_INTERVAL
        )
        unsub = async_track_time_interval(
            self.hass,
            self._checkpoint_callback,
            timedelta(minutes=opt_interval),
        )
        self._unsub_listeners.append(unsub)

        # 3) Price-source state change — re-run when tomorrow's prices arrive
        if self.config.get(CONF_PRICE_SOURCE, PRICE_SOURCE_EMALDO) == PRICE_SOURCE_EMALDO:
            # Watch Emaldo schedule_chart for tomorrow price updates
            emaldo_chart = self._resolve_emaldo_entity("schedule_chart")
            if emaldo_chart:
                unsub = async_track_state_change_event(
                    self.hass,
                    [emaldo_chart],
                    self._nordpool_state_change,
                )
                self._unsub_listeners.append(unsub)
                price_watcher_label = emaldo_chart
            else:
                price_watcher_label = "(emaldo chart not found)"
        else:
            spot_sensor = self.config.get(CONF_SPOT_SENSOR, "")
            if spot_sensor:
                unsub = async_track_state_change_event(
                    self.hass,
                    [spot_sensor],
                    self._nordpool_state_change,
                )
                self._unsub_listeners.append(unsub)
                price_watcher_label = spot_sensor
            else:
                price_watcher_label = "(no sensor configured)"

        # 4a) Balancing state watcher — replan when balancing ends
        self._balancing_sensor = self._find_balancing_sensor()
        if self._balancing_sensor:
            unsub = async_track_state_change_event(
                self.hass,
                [self._balancing_sensor],
                self._on_balancing_state_change,
            )
            self._unsub_listeners.append(unsub)
            _LOGGER.info(
                "Balancing watcher registered on %s", self._balancing_sensor
            )
        else:
            _LOGGER.debug("Balancing sensor not found — balancing replan disabled")

        # 4b) Low-SoC watcher — forced replan when actual SoC approaches the
        #     floor and the current plan has no imminent charge slot.  The
        #     replan lets the SoC safeguard insert a keep-alive charge.
        soc_sensor = self._resolve_emaldo_entity("battery_soc")
        if soc_sensor:
            unsub = async_track_state_change_event(
                self.hass,
                [soc_sensor],
                self._on_soc_state_change,
            )
            self._unsub_listeners.append(unsub)
            _LOGGER.info("Low-SoC watcher registered on %s", soc_sensor)
        else:
            _LOGGER.debug("Battery SoC sensor not found — low-SoC replan disabled")

        _LOGGER.info(
            "Listeners set up: midnight checkpoint + %d-min interval + price watcher on %s",
            opt_interval, price_watcher_label,
        )

        # 4) SoC Guard periodic timer
        guard_interval = self.config.get(
            CONF_SOC_GUARD_INTERVAL, DEFAULT_SOC_GUARD_INTERVAL
        )
        if guard_interval > 0:
            if self._unsub_guard is not None:
                self._unsub_guard()
            self._unsub_guard = async_track_time_interval(
                self.hass,
                self._soc_guard_callback,
                timedelta(minutes=guard_interval),
            )
            _LOGGER.info(
                "SoC guard timer set up: every %d minutes", guard_interval
            )
        else:
            if self._unsub_guard is not None:
                self._unsub_guard()
                self._unsub_guard = None

        # 5) Delayed startup run — populate sensors after restart
        unsub = async_call_later(
            self.hass, 90, self._startup_callback,
        )
        self._unsub_listeners.append(unsub)

        # 6) Track HA startup completion — suppresses race-condition warnings
        if not self._ha_started:
            unsub = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._on_ha_started
            )
            self._unsub_listeners.append(unsub)

        # 6) PV switch reconciliation — checks every 5 min that the switch
        #    matches the plan, catching restarts that lost transition timers
        unsub = async_track_time_interval(
            self.hass,
            self._pv_reconcile_callback,
            timedelta(minutes=5),
        )
        self._unsub_listeners.append(unsub)

    @callback
    def _on_ha_started(self, _event) -> None:
        """Mark that HA has fully started — enables startup-suppressed warnings."""
        self._ha_started = True

    @callback
    def _startup_callback(self, _now) -> None:
        """Run optimizer once after startup to restore sensor state."""
        if self._last_result is not None:
            return  # Already ran (e.g. Nordpool triggered first)
        _LOGGER.info("Startup delayed run — populating optimizer sensors")
        self.hass.async_create_task(
            self.run_optimizer(reason="startup", force=True)
        )

    @callback
    def _checkpoint_callback(self, now: datetime) -> None:
        """Checkpoint trigger — conditional re-optimization."""
        self.hass.async_create_task(
            self.run_optimizer(reason="checkpoint", force=False)
        )

    @callback
    def _nordpool_state_change(self, event) -> None:
        """Nordpool sensor changed — check if tomorrow's prices are now available."""
        if self._has_tomorrow_prices():
            _LOGGER.info("Nordpool tomorrow prices detected — running optimizer")
            self.hass.async_create_task(
                self.run_optimizer(reason="nordpool_update", force=True)
            )

    # ── SoC Guard ─────────────────────────────────────────────────────

    def _compute_soc_guard_marker(self) -> int:
        """Compute the SoC guard high_marker for the current window.

        Looks forward by the guard interval and finds the lowest planned
        discharge SoC in that window.  This prevents the battery from
        discharging below the planned level — even if unexpected loads
        (e.g. sauna) appear, the battery stops at the guard marker.

        Returns an integer percentage (1-100).
        """
        cfg = self._build_battery_config()
        if self._last_result is None:
            return int(cfg.soc_min)

        guard_interval = self.config.get(
            CONF_SOC_GUARD_INTERVAL, DEFAULT_SOC_GUARD_INTERVAL
        )
        if guard_interval <= 0:
            return int(cfg.soc_min)

        now_slot = _current_slot_index()
        interval_slots = max(guard_interval // 15, 1)
        end_slot = min(now_slot + interval_slots, SLOTS_PER_DAY - 1)
        plan = self._last_result.slots

        # Find the lowest discharge SoC target in this window
        min_soc: float | None = None
        for i in range(now_slot, min(end_slot + 1, len(plan))):
            if plan[i].action == "discharge":
                if min_soc is None or plan[i].soc_after < min_soc:
                    min_soc = plan[i].soc_after

        if min_soc is not None:
            return max(int(min_soc), int(cfg.soc_min))

        # No discharge in this window — use soc_min (most permissive).
        # This is safe because non-discharge slots (idle/charge) don't
        # draw from the battery via grid, so the marker is a no-op.
        return int(cfg.soc_min)

    @callback
    def _soc_guard_callback(self, now: datetime) -> None:
        """Periodic SoC guard timer — recompute and push updated marker."""
        if self._last_result is None or self._last_sent_slots is None:
            return
        self.hass.async_create_task(self._push_guard_update())

    async def _push_guard_update(self) -> None:
        """Recompute the SoC guard marker and resend if changed."""
        if self._last_sent_slots is None or self._last_result is None:
            return

        if not self.hass.services.has_service(EMALDO_DOMAIN, "apply_bulk_schedule"):
            return

        new_marker = self._compute_soc_guard_marker()
        if new_marker == self._current_guard_marker:
            _LOGGER.debug("SoC guard: high_marker unchanged at %d%%", new_marker)
            return

        cfg = self._build_battery_config()
        old_marker = self._current_guard_marker

        # Rebuild slot values with the new discharge byte
        slot_values = list(self._last_sent_slots)
        for i, val in enumerate(slot_values):
            if val > 0x80:  # discharge byte
                slot_values[i] = (256 - new_marker) & 0xFF

        self._current_guard_marker = new_marker
        self._last_sent_slots = slot_values

        _LOGGER.info(
            "SoC guard update: high_marker %d%% → %d%%",
            old_marker or 0, new_marker,
        )

        try:
            await self.hass.services.async_call(
                EMALDO_DOMAIN,
                "apply_bulk_schedule",
                {
                    "slots": slot_values,
                    "high_marker": new_marker,
                    "low_marker": int(cfg.soc_min),
                },
                blocking=True,
            )
        except Exception as err:
            _LOGGER.error("SoC guard push failed: %s", err)

    async def set_pv_strategy_enabled(self, enabled: bool, rerun: bool = True) -> None:
        """Set PV sell strategy enabled state.  Called by the switch entity.

        When rerun=True and the state changes, immediately re-runs the optimizer
        so the new strategy is reflected in the schedule and PV switch state.
        When rerun=False (e.g. called during HA startup restore), only updates
        the flag without triggering an optimizer run.
        """
        changed = self._pv_strategy_enabled != enabled
        self._pv_strategy_enabled = enabled
        if changed:
            _LOGGER.info("PV sell strategy %s", "enabled" if enabled else "disabled")
        if rerun and changed:
            await self.run_optimizer(reason="pv_strategy_changed", force=True)

    async def set_emaldo_control_enabled(self, enabled: bool, rerun: bool = True) -> None:
        """Set Emaldo control enabled state. Called by the switch entity.

        When enabled: optimizer can push schedules to the battery via apply_bulk_schedule.
        When disabled: optimizer runs but does not send any commands to Emaldo.

        When rerun=True and the state changes, immediately re-runs the optimizer.
        When rerun=False (e.g. called during HA startup restore), only updates
        the flag without triggering an optimizer run.
        """
        changed = self._emaldo_control_enabled != enabled
        self._emaldo_control_enabled = enabled
        if changed:
            _LOGGER.info("Emaldo control %s", "enabled" if enabled else "disabled")
        if rerun and changed:
            await self.run_optimizer(reason="emaldo_control_changed", force=True)

    async def _set_pv_switch(self, turn_on: bool) -> None:
        """Turn the Emaldo third-party PV switch on or off.

        The Emaldo unique_id key for the PV switch is ``thirdparty_pv_on``
        (domain: switch).  Falls back to ``switch.power_store_third_party_pv``
        if auto-discovery fails.
        """
        entity_id = self._resolve_emaldo_entity("thirdparty_pv_on", domain="switch")
        if entity_id is None:
            entity_id = "switch.power_store_third_party_pv"
            _LOGGER.debug(
                "PV switch auto-discovery failed — using fallback '%s'", entity_id
            )
        service = "turn_on" if turn_on else "turn_off"
        try:
            await self.hass.services.async_call(
                "switch", service, {"entity_id": entity_id}, blocking=True,
            )
            self._pv_switch_state = turn_on
            _LOGGER.debug("PV switch %s: %s", entity_id, service)
        except Exception as err:
            _LOGGER.error(
                "Failed to %s PV switch '%s': %s", service, entity_id, err
            )

    def _desired_pv_state_now(self, result: OptimizationResult | None) -> bool:
        """Return the effective desired PV switch state for the current slot.

        This folds in all runtime guards (strategy disabled, cloudy-day guard)
        so both immediate apply and periodic reconciliation use identical logic.
        """
        if not self._pv_strategy_enabled or result is None:
            return True

        # Solar forecast guard: skip strategy on cloudy days.
        solar_today = self._get_solcast_forecast("today")
        total_solar_kwh = sum(solar_today) * 0.25  # SLOT_DURATION_HOURS
        min_forecast = self.config.get(
            CONF_SOLAR_SELL_MIN_FORECAST_KWH, DEFAULT_SOLAR_SELL_MIN_FORECAST_KWH
        )
        if total_solar_kwh < min_forecast:
            return True

        now_slot = _current_slot_index()
        pv_slots = result.thirdparty_pv_slots
        return pv_slots[now_slot] if now_slot < len(pv_slots) else True

    async def _ensure_pv_switch_matches_plan(self, desired_on: bool) -> None:
        """Ensure actual Emaldo third-party PV switch state matches desired state."""
        entity_id = self._resolve_emaldo_entity("thirdparty_pv_on", domain="switch")
        if entity_id is None:
            entity_id = "switch.power_store_third_party_pv"

        state_obj = self.hass.states.get(entity_id)
        actual_on: bool | None = None
        if state_obj is not None and state_obj.state in ("on", "off"):
            actual_on = state_obj.state == "on"

        # Keep cache aligned with authoritative HA state when available.
        if actual_on is not None:
            self._pv_switch_state = actual_on

        if actual_on != desired_on:
            _LOGGER.warning(
                "PV switch mismatch: actual=%s, desired=%s — correcting",
                "on" if actual_on else "off",
                "on" if desired_on else "off",
            )
            await self._set_pv_switch(desired_on)

    async def _apply_pv_strategy(self, result: OptimizationResult) -> None:
        """Apply the PV sell strategy by controlling the Emaldo PV switch.

        Reads result.thirdparty_pv_slots for the current slot, applies the
        desired PV switch state immediately, then schedules async_call_later
        callbacks for each upcoming slot-boundary state transition.

        Guard: if today's total Solcast forecast < CONF_SOLAR_SELL_MIN_FORECAST_KWH,
        the strategy is skipped (keep PV on — cloudy day).
        """
        # Cancel all pending transition timers from the previous run.
        for unsub in self._unsub_pv_transitions:
            unsub()
        self._unsub_pv_transitions.clear()

        if not self._pv_strategy_enabled:
            # Strategy is off — restore PV to on.
            await self._ensure_pv_switch_matches_plan(True)
            return

        # Solar forecast guard: skip on cloudy days.
        solar_today = self._get_solcast_forecast("today")
        total_solar_kwh = sum(solar_today) * 0.25  # SLOT_DURATION_HOURS
        min_forecast = self.config.get(
            CONF_SOLAR_SELL_MIN_FORECAST_KWH, DEFAULT_SOLAR_SELL_MIN_FORECAST_KWH
        )
        if total_solar_kwh < min_forecast:
            _LOGGER.debug(
                "PV strategy: solar forecast %.1f kWh < %.1f kWh threshold — "
                "strategy inactive today",
                total_solar_kwh, min_forecast,
            )
            await self._ensure_pv_switch_matches_plan(True)
            return

        now_slot = _current_slot_index()
        pv_slots = result.thirdparty_pv_slots

        # Apply current slot's desired state.
        current_pv_on = pv_slots[now_slot] if now_slot < len(pv_slots) else True
        await self._ensure_pv_switch_matches_plan(current_pv_on)

        # Schedule transition callbacks for each upcoming state change.
        now = dt_util.now()
        n_transitions = 0
        for s in range(now_slot + 1, SLOTS_PER_DAY):
            desired = pv_slots[s] if s < len(pv_slots) else True
            prev_desired = pv_slots[s - 1] if (s - 1) < len(pv_slots) else True
            if desired == prev_desired:
                continue  # No state change at this boundary

            slot_start_hour = (s * 15) // 60
            slot_start_min = (s * 15) % 60
            target_dt = now.replace(
                hour=slot_start_hour,
                minute=slot_start_min,
                second=0,
                microsecond=0,
            )
            if target_dt <= now:
                continue  # Already past (shouldn't happen for s > now_slot)

            delay_seconds = (target_dt - now).total_seconds()
            pv_on = desired

            def _make_transition_cb(switch_on: bool) -> CALLBACK_TYPE:
                @callback
                def _transition(_now) -> None:
                    self.hass.async_create_task(self._set_pv_switch(switch_on))
                return _transition

            unsub = async_call_later(
                self.hass, delay_seconds, _make_transition_cb(pv_on)
            )
            self._unsub_pv_transitions.append(unsub)
            n_transitions += 1
            _LOGGER.debug(
                "PV transition at slot %d (%02d:%02d): PV %s",
                s, slot_start_hour, slot_start_min,
                "on" if pv_on else "off",
            )

        if n_transitions:
            _LOGGER.info(
                "PV sell strategy: %d transitions scheduled", n_transitions
            )

    @callback
    def _pv_reconcile_callback(self, _now) -> None:
        """Periodic check: correct the PV switch if it diverged from the plan."""
        if not self._pv_strategy_enabled or self._last_result is None:
            return
        self.hass.async_create_task(self._reconcile_pv_switch())

    async def _reconcile_pv_switch(self) -> None:
        """Verify the PV switch state matches the current plan and fix if not.

        Reads the actual entity state from HA rather than relying on the internal
        _pv_switch_state cache.  This catches cases where the service call was
        accepted by HA but the Emaldo device did not apply it (e.g. dropped
        connection), which would leave _pv_switch_state out of sync with reality.
        """
        if self._last_result is None:
            return
        desired = self._desired_pv_state_now(self._last_result)
        await self._ensure_pv_switch_matches_plan(desired)

    @callback
    def async_shutdown(self) -> None:
        """Clean up listeners."""
        for unsub in self._unsub_pv_transitions:
            unsub()
        self._unsub_pv_transitions.clear()
        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()
        if self._unsub_guard is not None:
            self._unsub_guard()
            self._unsub_guard = None

    async def _async_update_data(self) -> dict[str, Any]:
        """DataUpdateCoordinator callback — returns current state."""
        return {
            "result": self._last_result,
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "reason": self._last_reason,
            "activated_time": self._activated_time,
        }
