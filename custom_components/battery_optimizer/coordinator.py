"""Data coordinator for Battery Optimizer.

Gathers data from Nordpool, Solcast, and battery sensors, runs the optimizer,
and pushes the resulting schedule to the Emaldo integration.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
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
    SUBENTRY_TYPE_RULE,
    SUBENTRY_TYPE_DEVICE,
    SLOTS_PER_DAY,
    SLOT_DURATION_HOURS,
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
    IDLE_GAP_RERUN_THROTTLE_MIN,
    SOC_DIVERGENCE_RERUN_THRESHOLD_PCT,
    SOC_DIVERGENCE_RERUN_THROTTLE_MIN,
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
    CONF_SOLAR_FORECAST_SCALE,
    CONF_SOLAR_ACTUAL_SENSOR,
    SOLAR_FORECAST_P10,
    DEFAULT_AUTO_BASE_LOAD,
    DEFAULT_LOAD_ENERGY_SENSOR,
    DEFAULT_ENABLE_PV_STRATEGY,
    DEFAULT_ENABLE_EMALDO_CONTROL,
    DEFAULT_SOLAR_SELL_MIN_FORECAST_KWH,
    DEFAULT_SOLAR_FORECAST_MODE,
    DEFAULT_SOLAR_FORECAST_SCALE,
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
    SlotPlan,
    compute_prices,
    interpolate_solar_to_15min,
    optimize,
    _simulate_soc_trajectory,
)
from .rules import (
    UserRule,
    rule_from_data,
    expand_day,
    mask_plan,
    LEVEL_DEFAULT,
    SlotWinner,
)
from .solar_scale import resolve_solar_scale
from .solar_regime import default_state as solar_regime_default_state
from .solar_regime import deserialize as solar_regime_deserialize
from .solar_regime import update_regime
from .solar_actual import counter_delta_kwh, normalize_to_kwh, resolve_solar_source
from .runtime_state import prune_plan_slots, rebuild_runtime, serialize_runtime

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
        self._unsub_ha_started: CALLBACK_TYPE | None = None
        self._unsub_startup: CALLBACK_TYPE | None = None
        self._startup_attempts: int = 0
        self._last_result: OptimizationResult | None = None
        self._last_result_tomorrow: OptimizationResult | None = None
        self._last_run: datetime | None = None
        self._last_reason: str = ""
        self._last_sources: list[str] | None = None
        self._last_user_winners: list[SlotWinner] | None = None
        self._last_pv_sources: list[str] | None = None
        self._last_sources_tomorrow: list[str] | None = None
        self._last_user_winners_tomorrow: list[SlotWinner] | None = None
        self._last_pv_sources_tomorrow: list[str] | None = None
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
        # Persisted accuracy history — JSON sidecar in the HA config dir
        # (survives restarts; the HA recorder strips sensor attributes, so
        # per-run planned-vs-actual values would otherwise be lost).
        self._accuracy_history_path = self.hass.config.path(
            "battery_optimizer_accuracy.json"
        )
        self._accuracy_history: list[dict] | None = None
        # Persisted last-run runtime state — plan + snapshot survive restarts
        # so the first accuracy compute after boot has data immediately.
        self._runtime_state_path = self.hass.config.path(
            "battery_optimizer_runtime.json"
        )
        self._runtime_state_restored = False
        # Solar forecast scale — resolved per run, applied at the forecast
        # choke point. The last value used by a plan run is retained so the
        # NEXT accuracy computation can attribute the error to the scale that
        # actually produced the slots being measured.
        self._solar_scale: float = 1.0
        self._last_result_solar_scale: float = 1.0
        # Durable solar regime — persisted EWMA over daily scaled-forecast
        # solar fraction of the usable band; engaged flag gates the Case A
        # discharge floor (see solar_regime.py).  Loaded lazily per run.
        self._solar_regime_state_path = self.hass.config.path(
            "battery_optimizer_solar_regime.json"
        )
        self._solar_regime_state: dict | None = None
        self._solar_regime: dict | None = None
        self._solar_regime_fraction: float | None = None
        self._solar_regime_band_kwh: float | None = None
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
        # L2 idle-gap replan throttle
        self._last_idle_gap_rerun: datetime | None = None
        # L3 divergence replan throttle
        self._last_divergence_rerun: datetime | None = None
        # Startup flag — suppresses false-positive warnings before HA has fully started
        self._ha_started: bool = False
        # Emaldo device identity — resolved from hass.data at setup for device_info
        self._emaldo_device_id: str | None = None
        self._emaldo_device_name: str | None = None

    @property
    def config(self) -> dict[str, Any]:
        """Merged config data + options."""
        return {**self._entry.data, **self._entry.options}

    def _config_int(self, key: str, fallback: int) -> int:
        """Return a config value as an int, falling back when unavailable."""
        value = self.config.get(key, fallback)
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

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

    # ── Device info ───────────────────────────────────────────────────

    def resolve_emaldo_device(self) -> bool:
        """Resolve the linked Emaldo device's ID and name from hass.data.

        Called lazily on first ``device_info`` access, or explicitly from
        the startup listener in ``__init__.py``.  Returns ``True`` if
        Emaldo data was found, ``False`` if still unavailable.
        Does NOT trigger a reload — the caller is responsible for that.
        """
        if self._emaldo_device_id is not None:
            return True

        emaldo_data = self.hass.data.get(EMALDO_DOMAIN)
        if not emaldo_data or not self._emaldo_entry_id:
            return False
        entry_data = emaldo_data.get(self._emaldo_entry_id)
        if entry_data is None:
            return False
        coord = entry_data.get("power")
        if coord is None:
            return False
        self._emaldo_device_id = getattr(coord, "device_id", None)
        self._emaldo_device_name = getattr(coord, "device_name", None)
        if self._emaldo_device_id is not None:
            _LOGGER.info(
                "Resolved Emaldo device: id=%s name=%s",
                self._emaldo_device_id,
                self._emaldo_device_name,
            )
        return self._emaldo_device_id is not None

    @property
    def device_info(self) -> "DeviceInfo | None":
        """Return device info for the virtual Battery Optimizer device.

        Lazily resolves the Emaldo device identity on first access,
        since Battery Optimizer's ``async_setup_entry`` may run before
        the Emaldo integration has stored its data in ``hass.data``.
        """
        from homeassistant.helpers.device_registry import DeviceInfo  # noqa: PLC0415

        if self._emaldo_device_id is None:
            self.resolve_emaldo_device()
        if self._emaldo_device_id is None:
            return None
        return DeviceInfo(
            identifiers={(DOMAIN, self._emaldo_device_id)},
            name="Battery Optimizer Configuration",
            manufacturer="Emaldo",
            model="Optimized Battery",
            via_device=(EMALDO_DOMAIN, self._emaldo_device_id),
        )

    @property
    def soc_guard_marker(self) -> int | None:
        """Current SoC guard high_marker, or None if guard is disabled."""
        guard_interval = self._config_int(
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

        # L3 — divergence check (independent of the SoC safeguard): actual SoC
        # has drifted more than the threshold from what the current plan
        # projects for this slot.  Catches forecast error / unexpected loads /
        # cheap-day under-discharge between the polled checkpoints.  Throttled
        # so a SoC sensor that updates every few seconds cannot storm replans.
        if self._last_result is not None:
            now_slot = _current_slot_index()
            if now_slot < len(self._last_result.slots):
                planned_soc = self._last_result.slots[now_slot].soc_after
                if abs(soc - planned_soc) > SOC_DIVERGENCE_RERUN_THRESHOLD_PCT:
                    now = dt_util.now()
                    if (
                        self._last_divergence_rerun is not None
                        and (now - self._last_divergence_rerun).total_seconds()
                        < SOC_DIVERGENCE_RERUN_THROTTLE_MIN * 60
                    ):
                        return
                    self._last_divergence_rerun = now
                    _LOGGER.info(
                        "SoC divergence: actual=%.1f%%, planned=%.1f%% "
                        "(>%.0f%%) — forcing replan",
                        soc, planned_soc, SOC_DIVERGENCE_RERUN_THRESHOLD_PCT,
                    )
                    self.hass.async_create_task(
                        self.run_optimizer(reason="soc_divergence", force=True)
                    )
                    return

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
        is_cents = (
            "snt" in unit or "cent" in unit or "c/kwh" in unit
            or "öre" in unit or "øre" in unit
        )

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
        forecast_15 = interpolate_solar_to_15min(slots_30min)
        # Whole-day over-forecast compensation. The constant multiplier
        # commutes with the linear interpolation, so scaling AFTER it yields
        # the same profile as scaling the 30-min slots.
        return [v * self._solar_scale for v in forecast_15]

    def _resolve_solar_scale(self) -> float:
        """Resolve the whole-day solar scale: manual config or auto-tune.

        Runs inside an executor (sync file I/O when the sidecar history has
        not been loaded yet). ``0.0`` in config means "auto": tune the scale
        from the persisted accuracy history via EWMA over raw-basis ratios.
        """
        configured = self.config.get(
            CONF_SOLAR_FORECAST_SCALE, DEFAULT_SOLAR_FORECAST_SCALE
        )
        if self._accuracy_history is None:
            self._accuracy_history = self._load_accuracy_history()
        return resolve_solar_scale(configured, self._accuracy_history)

    def _load_solar_regime_state(self) -> dict:
        """Load persisted solar-regime state; cold-start defaults on garbage."""
        try:
            with open(self._solar_regime_state_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = None
        return solar_regime_deserialize(data)

    def _save_solar_regime_state(self, state: dict) -> None:
        """Write the solar-regime state to its JSON sidecar file."""
        try:
            with open(self._solar_regime_state_path, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
        except OSError:
            _LOGGER.warning(
                "Could not write solar regime state to %s",
                self._solar_regime_state_path,
            )

    def _resolve_solar_regime(self, forecast_kwh: float, cfg) -> dict:
        """One-per-day solar-regime EWMA step; engaged flag for the gate.

        Runs inside an executor (sync file I/O when the sidecar state has not
        been loaded yet).  ``forecast_kwh`` is the day's total scaled solar
        forecast; the fraction is relative to the user's own usable band
        ``(soc_max - soc_min) * capacity``.  The update is date-guarded: the
        same-date call returns the input object unchanged and no write
        happens (no intra-day oscillation).
        """
        if self._solar_regime_state is None:
            self._solar_regime_state = self._load_solar_regime_state()
        band_kwh = cfg.capacity_kwh * (cfg.soc_max - cfg.soc_min) / 100.0
        self._solar_regime_band_kwh = band_kwh
        self._solar_regime_fraction = (
            forecast_kwh / band_kwh if band_kwh > 0.0 else None
        )
        state = update_regime(
            self._solar_regime_state, forecast_kwh, band_kwh,
            str(dt_util.now().date()),
        )
        if state is not self._solar_regime_state:
            self._solar_regime_state = state
            self._save_solar_regime_state(state)
        return state

    def _resolve_emaldo_entity(self, key: str, domain: str = "sensor") -> str | None:
        """Resolve an Emaldo entity_id from the entity registry.

        Emaldo constructs its entities' unique_ids from the coordinator's
        ``device_id`` on current versions and from ``home_id`` on older ones,
        so both bases are tried: ``{device_id}_{key}`` first, then
        ``{home_id}_{key}``.  This works for any Emaldo device model
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
        registry = er.async_get(self.hass)
        for entry_id, entry_data in entries:
            coord = entry_data.get("power")  # EmaldoCoordinator — holds ids
            if coord is None:
                continue
            # Current ha-emaldo versions derive unique_ids from device_id;
            # legacy ones from home_id. Try both so auto-discovery works
            # across versions without hardcoding either scheme (#9).
            for uid_base in (
                getattr(coord, "device_id", None),
                getattr(coord, "home_id", None),
            ):
                if not uid_base:
                    continue
                unique_id = f"{uid_base}_{key}"
                entity_id = registry.async_get_entity_id(
                    domain, EMALDO_DOMAIN, unique_id
                )
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

    def _read_external_solar_kwh(self) -> float | None:
        """Read the configured actual-solar counter, normalized to kWh.

        Returns None when not configured, unavailable, non-numeric, or the
        unit is neither Wh nor kWh — the caller decides fallback vs skip.
        """
        sensor_id = self.config.get(CONF_SOLAR_ACTUAL_SENSOR, "")
        if not sensor_id:
            return None
        state = self.hass.states.get(sensor_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        return normalize_to_kwh(value, state.attributes.get("unit_of_measurement"))

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
        ext_now = self._read_external_solar_kwh()
        solar_source = resolve_solar_source(
            self.config.get(CONF_SOLAR_ACTUAL_SENSOR, "") != "",
            "solar_ext" in snap and snap["solar_ext"] is not None,
            ext_now is not None,
        )
        if solar_source == "skip":
            _LOGGER.warning(
                "External solar sensor (%s) unavailable — skipping accuracy "
                "record to keep auto-tune training data single-sourced",
                self.config.get(CONF_SOLAR_ACTUAL_SENSOR, ""),
            )
            return None

        accuracy: dict = {
            "elapsed_slots": elapsed,
            "planned_discharge_kwh": round(planned_discharge, 3),
            "planned_charge_kwh": round(planned_charge, 3),
            "planned_solar_kwh": round(planned_solar, 3),
            "solar_scale_used": round(self._last_result_solar_scale, 3),
            "solar_source": solar_source,
            "last_run": self._last_run.isoformat() if self._last_run else None,
        }

        emaldo_pairs = [
            ("battery_discharged_today", "discharge"),
            ("battery_charged_today", "charge"),
        ]
        if solar_source == "emaldo":
            emaldo_pairs.append(("solar_energy_today", "solar"))
        for emaldo_key, snap_key in emaldo_pairs:
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
                else:
                    # Daily counter reset within the window: the snapshot was
                    # taken pre-reset (midnight run) so the counter is now lower.
                    # The pre-reset fraction is unmeasurable via daily counters —
                    # use the post-reset accumulation as best-effort actual and
                    # flag the window so consumers can distinguish it.
                    actual_kwh = round(max(actual, 0.0), 3)
                    accuracy[f"actual_{snap_key}_kwh"] = actual_kwh
                    accuracy[f"{snap_key}_error_kwh"] = round(
                        actual_kwh - accuracy[f"planned_{snap_key}_kwh"], 3
                    )
                    accuracy[f"{snap_key}_reset_crossed"] = True

        if solar_source == "external":
            ext_delta, ext_reset = counter_delta_kwh(snap["solar_ext"], ext_now)
            accuracy["actual_solar_kwh"] = round(ext_delta, 3)
            accuracy["solar_error_kwh"] = round(
                ext_delta - accuracy["planned_solar_kwh"], 3
            )
            if ext_reset:
                accuracy["solar_reset_crossed"] = True

        return accuracy

    # ── Accuracy history (persisted JSON sidecar) ──────────────────────

    _ACCURACY_HISTORY_MAX_RECORDS = 1000
    _ACCURACY_HISTORY_MAX_AGE_DAYS = 60

    # Startup run — short grace, then retry while Emaldo SoC is unreadable.
    _STARTUP_GRACE_S = 15
    _STARTUP_RETRY_DELAY_S = 30
    _STARTUP_MAX_ATTEMPTS = 4

    def _load_accuracy_history(self) -> list[dict]:
        """Load persisted accuracy history from the JSON sidecar file."""
        try:
            with open(self._accuracy_history_path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, list):
                return []
            return [r for r in data if isinstance(r, dict)]
        except (OSError, ValueError):
            return []

    def _save_accuracy_history(self) -> None:
        """Write the accuracy history to the JSON sidecar file."""
        try:
            with open(self._accuracy_history_path, "w", encoding="utf-8") as fh:
                json.dump(self._accuracy_history or [], fh)
        except OSError:
            _LOGGER.warning(
                "Could not write accuracy history to %s", self._accuracy_history_path
            )

    def _load_runtime_state(self) -> dict | None:
        """Load persisted last-run runtime state; None when unusable."""
        try:
            with open(self._runtime_state_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _save_runtime_state(self) -> None:
        """Persist the current last-run plan + snapshot for restart survival."""
        if not self._last_result or self._last_run_slot is None or not self._last_run:
            return
        try:
            with open(self._runtime_state_path, "w", encoding="utf-8") as fh:
                json.dump(
                    serialize_runtime(
                        last_run_slot=self._last_run_slot,
                        last_run_initial_soc=self._last_run_initial_soc,
                        last_run_scale=self._last_result_solar_scale,
                        last_run_ts=self._last_run.isoformat(),
                        snapshot=self._last_run_actual_snapshot or {},
                        plan_slots=prune_plan_slots(
                            self._last_result.slots, self._last_run_slot
                        ),
                    ),
                    fh,
                )
        except OSError:
            _LOGGER.warning(
                "Could not write last-run runtime state to %s",
                self._runtime_state_path,
            )

    async def _restore_runtime_state(self) -> None:
        """Rehydrate the previous run's plan + snapshot after an HA restart.

        Idempotent. Stale-day payloads (rebuild_runtime returns None) leave the
        coordinator in the normal cold-start state (accuracy needs 2 runs).
        File I/O runs on the executor — never blocks the event loop.
        """
        if self._runtime_state_restored:
            return
        self._runtime_state_restored = True
        data = await self.hass.async_add_executor_job(self._load_runtime_state)
        restored = rebuild_runtime(data, dt_util.now()) if data else None
        if restored is None:
            _LOGGER.info(
                "No usable last-run runtime state — accuracy chain starts fresh"
            )
            return
        self._last_run_slot = restored["last_run_slot"]
        self._last_run_initial_soc = restored["last_run_initial_soc"]
        self._last_result_solar_scale = restored["last_run_scale"]
        self._last_run = dt_util.parse_datetime(restored["last_run"])
        self._last_run_actual_snapshot = dict(restored["snapshot"])
        self._last_result = OptimizationResult(
            slots=[
                SlotPlan(
                    index=e[0],
                    action=e[1],
                    slot_value=0,
                    buy_price=0.0,
                    sell_price=0.0,
                    soc_after=e[2],
                    solar_kw=e[3],
                )
                for e in restored["plan_slots"]
            ]
        )
        _LOGGER.info(
            "Restored last-run runtime state: slot %d, %d plan slots, scale %.3f",
            self._last_run_slot,
            len(restored["plan_slots"]),
            self._last_result_solar_scale,
        )

    def _prune_accuracy_history(self) -> None:
        """Drop records older than the retention window and cap the list."""
        cutoff = (
            dt_util.now() - timedelta(days=self._ACCURACY_HISTORY_MAX_AGE_DAYS)
        ).isoformat()
        self._accuracy_history = [
            r
            for r in self._accuracy_history
            if r.get("ts", "") >= cutoff
        ][-self._ACCURACY_HISTORY_MAX_RECORDS:]

    def _accuracy_summary(self) -> dict:
        """Rolling summary of planned-vs-actual accuracy over the window.

        ``solar_error_kwh`` sign convention: ``actual - planned``. Negative
        error = actual below forecast (forecast over-optimistic); positive =
        actual above forecast (forecast conservative, e.g. P10).
        """
        hist = self._accuracy_history or []
        if not hist:
            return {"runs": 0, "window_days": 0.0, "mean_solar_error_kwh": None}

        solar_errs = [
            r["solar_error_kwh"]
            for r in hist
            if isinstance(r.get("solar_error_kwh"), (int, float))
        ]
        disch_errs = [
            r["discharge_error_kwh"]
            for r in hist
            if isinstance(r.get("discharge_error_kwh"), (int, float))
        ]
        span_days = 0.0
        timestamps = [r.get("ts", "") for r in hist if r.get("ts")]
        if len(timestamps) >= 2:
            try:
                t0 = dt_util.parse_datetime(timestamps[0])
                t1 = dt_util.parse_datetime(timestamps[-1])
                if t0 and t1:
                    span_days = round((t1 - t0).total_seconds() / 86400, 1)
            except (TypeError, ValueError):
                pass

        summary: dict[str, Any] = {
            "runs": len(hist),
            "window_days": span_days,
            "mean_solar_error_kwh": (
                round(sum(solar_errs) / len(solar_errs), 3) if solar_errs else None
            ),
            "solar_under_forecast_runs": sum(1 for e in solar_errs if e < 0),
            "solar_over_forecast_runs": sum(1 for e in solar_errs if e > 0),
        }
        if disch_errs:
            summary["mean_discharge_error_kwh"] = round(
                sum(disch_errs) / len(disch_errs), 3
            )
        return summary

    async def _record_accuracy(self, accuracy: dict) -> None:
        """Persist one planned-vs-actual record and refresh the summary.

        The summary is injected into ``accuracy["accuracy_history"]`` so the
        Plan Accuracy sensor exposes the rolling trend without extra entities.
        """
        if self._accuracy_history is None:
            self._accuracy_history = await self.hass.async_add_executor_job(
                self._load_accuracy_history
            )
        self._accuracy_history.append(
            {
                "ts": dt_util.now().isoformat(timespec="seconds"),
                "elapsed_slots": accuracy.get("elapsed_slots"),
                "planned_solar_kwh": accuracy.get("planned_solar_kwh"),
                "actual_solar_kwh": accuracy.get("actual_solar_kwh"),
                "solar_error_kwh": accuracy.get("solar_error_kwh"),
                "solar_scale_used": accuracy.get("solar_scale_used"),
                "solar_source": accuracy.get("solar_source"),
                "solar_reset_crossed": accuracy.get("solar_reset_crossed"),
                "planned_discharge_kwh": accuracy.get("planned_discharge_kwh"),
                "actual_discharge_kwh": accuracy.get("actual_discharge_kwh"),
                "discharge_error_kwh": accuracy.get("discharge_error_kwh"),
            }
        )
        self._prune_accuracy_history()
        accuracy["accuracy_history"] = self._accuracy_summary()
        await self.hass.async_add_executor_job(self._save_accuracy_history)

    # ── Optimizer entry point ─────────────────────────────────────────

    async def run_optimizer(
        self, reason: str = "manual", force: bool = True
    ) -> OptimizationResult | None:
        """Run the optimizer and push the schedule to Emaldo.

        Args:
            reason: Why this run was triggered.
            force: If False, skip if conditions haven't changed enough.
        """
        await self._restore_runtime_state()
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

        # Solar forecast scale — manual config or auto-tuned from the accuracy
        # sidecar (executor: sync file I/O). Applied inside _get_solcast_forecast.
        self._solar_scale = await self.hass.async_add_executor_job(
            self._resolve_solar_scale
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
            await self._record_accuracy(new_accuracy)
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

        # Durable solar regime — once-per-day EWMA over the scaled-forecast
        # solar fraction; engaged → Case A discharge must beat the cheapest
        # known future recharge (executor: sync file I/O on first load).
        self._solar_regime = await self.hass.async_add_executor_job(
            self._resolve_solar_regime, sum(solar) * SLOT_DURATION_HOURS, cfg
        )

        # Run optimizer — prices_today is already 96 x 15-min in €/kWh
        buy_prices, sell_prices = compute_prices(prices_today, cfg)
        # Tomorrow's prices (published ~13:00 CET, sensor-triggered re-run)
        # must be ready BEFORE today's plan: when the no-refill regime is
        # engaged, today's discharge floor is min(remaining today, tomorrow).
        buy_tom = None
        sell_tom = None
        if prices_tomorrow is not None:
            buy_tom, sell_tom = compute_prices(prices_tomorrow, cfg)
        emaldo_modes = self._read_emaldo_internal_modes()
        result = optimize(
            buy_prices,
            sell_prices,
            solar,
            cfg,
            start_slot=now_slot,
            initial_soc_pct=soc,
            enable_pv_strategy=self._pv_strategy_enabled,
            emaldo_modes=emaldo_modes,
            solar_regime_engaged=bool(
                self._solar_regime and self._solar_regime["engaged"]
            ),
            future_min_buy=min(buy_tom) if buy_tom else None,
        )
        result.reason = reason

        # ── User schedule layer: apply rules as a mask ──────────────
        user_rules = self._read_user_rules()
        today_date = dt_util.now().date()
        if any(r.action != "optimizer" or r.level != LEVEL_DEFAULT
               for r in user_rules):
            self._apply_user_mask(
                result, user_rules, solar, cfg, start_slot=now_slot,
                initial_soc_kwh=cfg.capacity_kwh * soc / 100.0,
                day=today_date,
            )
        else:
            # only the default optimizer rule — pure fast path, unchanged
            self._last_sources = None
            self._last_user_winners = None

        self._last_result = result
        self._last_run = dt_util.now()
        self._last_reason = reason
        # Retain the scale THIS plan ran under — the next run's accuracy
        # comparison attributes the solar error to it (raw-basis recovery).
        self._last_result_solar_scale = self._solar_scale

        # Snapshot actual values for next accuracy comparison
        self._last_run_slot = now_slot
        self._last_run_initial_soc = soc
        self._last_run_actual_snapshot = {
            "discharge": self._read_emaldo_sensor_float("battery_discharged_today"),
            "charge": self._read_emaldo_sensor_float("battery_charged_today"),
            "solar": self._read_emaldo_sensor_float("solar_energy_today"),
        }
        if self.config.get(CONF_SOLAR_ACTUAL_SENSOR, ""):
            # None when unavailable → accuracy skips the record rather than
            # mixing the Emaldo estimate into the external-source series.
            self._last_run_actual_snapshot["solar_ext"] = (
                self._read_external_solar_kwh()
            )

        await self.hass.async_add_executor_job(self._save_runtime_state)

        # Optimize tomorrow if prices available
        if prices_tomorrow is not None:
            solar_tomorrow = self._get_solcast_forecast("tomorrow")
            end_soc = result.slots[-1].soc_after if result.slots else None
            result_tomorrow = optimize(
                buy_tom,
                sell_tom,
                solar_tomorrow,
                cfg,
                start_slot=0,
                initial_soc_pct=end_soc,
                enable_pv_strategy=self._pv_strategy_enabled,
                emaldo_modes=emaldo_modes,
                solar_regime_engaged=bool(
                    self._solar_regime and self._solar_regime["engaged"]
                ),
            )
            self._last_result_tomorrow = result_tomorrow
            self._apply_user_mask(
                result_tomorrow, user_rules, solar_tomorrow, cfg,
                start_slot=0,
                initial_soc_kwh=cfg.capacity_kwh * (
                    result.slots[-1].soc_after if result.slots else cfg.soc_min
                ) / 100.0,
                day=(today_date + timedelta(days=1)),
                store_last=False,
                store_last_tomorrow=True,
            )
            _LOGGER.info(
                "Tomorrow optimization: savings=%.4f€, C=%d D=%d I=%d",
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

    def _device_subentry_id(self) -> str | None:
        """Return the 'device' container subentry id for this entry, if any."""
        for sub in self._entry.subentries.values():
            if sub.subentry_type == SUBENTRY_TYPE_DEVICE:
                return sub.subentry_id
        return None

    def _read_user_rules(self) -> list[UserRule]:
        """Read schedule rules from config subentries."""
        rules: list[UserRule] = []
        try:
            for sub in self._entry.subentries.values():
                if sub.subentry_type != SUBENTRY_TYPE_RULE:
                    continue
                try:
                    rules.append(rule_from_data(dict(sub.data)))
                except Exception:
                    _LOGGER.warning(
                        "Skipping invalid schedule rule subentry %s", sub.subentry_id
                    )
        except Exception:
            _LOGGER.warning("Could not read schedule rule subentries")
        if not any(r.level == LEVEL_DEFAULT for r in rules):
            rules.append(rule_from_data({
                "level": LEVEL_DEFAULT, "days": [], "start_date": None,
                "end_date": None, "start_time": "00:00", "end_time": "24:00",
                "action": "optimizer", "soc_target": None,
                "pv_sell": "inherit", "label": "Default",
            }))
        return rules

    def _apply_user_mask(
        self,
        result: OptimizationResult,
        rules: list[UserRule],
        solar_15min: list[float],
        cfg: BatteryConfig,
        *,
        start_slot: int,
        initial_soc_kwh: float,
        day: date,
        store_last: bool = True,
        store_last_tomorrow: bool = False,
    ) -> None:
        """Apply user rules to a result in place: bytes, PV, actions, SoC.

        Slots where the winning rule is a manual action or 'original' get
        their byte overwritten; optimizer/empty slots keep the plan.  The
        SoC trajectory is re-simulated from the masked actions so
        soc_after, tomorrow's start SoC, the SoC guard and the dashboard
        forecast all reflect what will actually be pushed.
        """
        winners = expand_day(rules, day)
        masked, masked_pv, sources, pv_sources = mask_plan(
            result.slot_values, result.thirdparty_pv_slots, winners
        )
        if store_last:
            self._last_user_winners = winners
            self._last_sources = sources
            self._last_pv_sources = pv_sources
        if store_last_tomorrow:
            self._last_user_winners_tomorrow = winners
            self._last_sources_tomorrow = sources
            self._last_pv_sources_tomorrow = pv_sources

        n_charge = n_discharge = n_idle = 0
        charge_targets: dict[int, int] = {}
        discharge_targets: dict[int, int] = {}
        for sp in result.slots:
            if sp.index < start_slot:
                continue
            byte = masked[sp.index]
            sp.slot_value = byte
            if byte == 0:
                sp.action = "idle"
                n_idle += 1
            elif byte > 0x80:
                sp.action = "discharge"
                n_discharge += 1
                discharge_targets[sp.index] = 256 - byte
            elif byte == 0x80:
                sp.action = "idle"  # original: AI decides; count as idle
                n_idle += 1
            else:  # 1..100
                sp.action = "charge"
                n_charge += 1
                charge_targets[sp.index] = byte

        plan_actions = {sp.index: sp.action for sp in result.slots}
        net_loads = [cfg.base_load_kw - solar_15min[i] for i in range(96)]
        traj_kwh = _simulate_soc_trajectory(
            plan_actions, net_loads, solar_15min, cfg,
            start_slot=start_slot, initial_soc_kwh=initial_soc_kwh,
            charge_targets=charge_targets,
            discharge_targets=discharge_targets,
        )
        for sp in result.slots:
            if sp.index < len(traj_kwh):
                sp.soc_after = round(traj_kwh[sp.index] / cfg.capacity_kwh * 100.0, 1)
        result.thirdparty_pv_slots = masked_pv
        result.charge_slots = n_charge
        result.discharge_slots = n_discharge
        result.idle_slots = n_idle

    @property
    def last_sources(self) -> list[str] | None:
        """Per-slot sources ('user'/'internal'/'optimizer') for today."""
        return self._last_sources

    @property
    def last_pv_sources(self) -> list[str] | None:
        """Per-slot PV sources ('user' where a rule set PV, else 'optimizer')."""
        return self._last_pv_sources

    @property
    def last_sources_tomorrow(self) -> list[str] | None:
        """Per-slot sources for tomorrow's plan."""
        return self._last_sources_tomorrow

    @property
    def last_user_winners_tomorrow(self) -> list[SlotWinner] | None:
        """Winning rule decisions for tomorrow's plan."""
        return self._last_user_winners_tomorrow

    @property
    def last_pv_sources_tomorrow(self) -> list[str] | None:
        """Per-slot PV sources for tomorrow's plan."""
        return self._last_pv_sources_tomorrow

    @property
    def last_user_winners(self) -> list[SlotWinner] | None:
        """Winning rule decisions for today's plan."""
        return self._last_user_winners

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

        # L2 — idle-gap gate: the plan leaves the current slot idle while the
        # grid is actually buying at a price above wear cost and the battery
        # has headroom.  A re-run with the real (possibly higher) SoC lets the
        # discharge allocation open the slot (SPLIT budget / plateau-edge
        # shift) that the plan-time projection kept closed.
        sp = self._last_result.slots[now_slot]
        if (
            sp.action in ("idle", "none")
            and sp.buy_price > cfg.wear_cost_per_kwh
            and sp.load_kw > sp.solar_kw + 0.01  # grid buying this slot
            and current_soc > cfg.soc_min + LOW_SOC_RERUN_MARGIN_PCT
        ):
            now = dt_util.now()
            if (
                self._last_idle_gap_rerun is not None
                and (now - self._last_idle_gap_rerun).total_seconds()
                < IDLE_GAP_RERUN_THROTTLE_MIN * 60
            ):
                return False
            self._last_idle_gap_rerun = now
            _LOGGER.info(
                "Idle-gap: plan idle @ %.1f%% while grid buys %.3f€/kWh "
                "(wear %.3f€/kWh) — re-optimizing",
                current_soc, sp.buy_price, cfg.wear_cost_per_kwh,
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
        guard_interval = self._config_int(
            CONF_SOC_GUARD_INTERVAL, DEFAULT_SOC_GUARD_INTERVAL
        )
        soc_guard_enabled = guard_interval > 0
        service_data: dict[str, Any] = {"slots": slot_values}
        if self._emaldo_device_id is None:
            self.resolve_emaldo_device()
        if self._emaldo_device_id:
            service_data["device_id"] = self._emaldo_device_id

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
        # Cancel any existing listeners (except one-time HA startup listener)
        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()

        # 0) Track HA startup completion — only register once, ever
        if not self._ha_started and self._unsub_ha_started is None:
            self._unsub_ha_started = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._on_ha_started
            )

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
        opt_interval = self._config_int(
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

        # 4c) SoC Guard periodic timer
        guard_interval = self._config_int(
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

        # 5) Delayed startup run — populate sensors after restart.  Short
        #    grace period + retry while the battery SoC is unreadable
        #    (Emaldo stream not yet established) — replaces the old blanket
        #    90s delay that burned a run during slow boots.
        self._startup_attempts = 0
        self._schedule_startup_run(self._STARTUP_GRACE_S)

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
        self._unsub_ha_started = None

    @callback
    def _schedule_startup_run(self, delay_s: int) -> None:
        """Arm the one-shot startup run timer; cancels any prior arm."""
        if self._unsub_startup is not None:
            self._unsub_startup()
        self._unsub_startup = async_call_later(
            self.hass, delay_s, self._startup_callback
        )

    @callback
    def _startup_callback(self, _now) -> None:
        """Run optimizer once after startup to restore sensor state.

        Retries with a growing delay while the battery SoC is unreadable
        (Emaldo stream still establishing).  After ``_STARTUP_MAX_ATTEMPTS``
        it gives up — the checkpoint interval / price watcher take over.
        """
        self._unsub_startup = None
        if self._last_result is not None:
            return  # Already ran (e.g. Nordpool triggered first)
        self._startup_attempts += 1
        if self._startup_attempts > self._STARTUP_MAX_ATTEMPTS:
            _LOGGER.warning(
                "Startup run gave up after %d attempts — battery SoC unreadable; "
                "checkpoint/price watchers will take over",
                self._STARTUP_MAX_ATTEMPTS,
            )
            return
        if self._get_battery_soc() is None:
            _LOGGER.info(
                "Startup run attempt %d — battery SoC not ready, retrying in %ds",
                self._startup_attempts, self._STARTUP_RETRY_DELAY_S,
            )
            self._schedule_startup_run(self._STARTUP_RETRY_DELAY_S)
            return
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

        guard_interval = self._config_int(
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
            marker = max(int(min_soc), int(cfg.soc_min))
        else:
            # No discharge in this window — use soc_min (most permissive).
            # This is safe because non-discharge slots (idle/charge) don't
            # draw from the battery via grid, so the marker is a no-op.
            marker = int(cfg.soc_min)

        # User rule floors: a user discharge@N rule in the look-ahead
        # window is authoritative — never drain below it.  The user's
        # floor is a "never below N" constraint, so it RAISES the marker
        # (the optimizer-derived floor is only a lower bound).  Using
        # min() here would let an optimizer discharge@15 elsewhere in the
        # window pull the marker down and the guard's global remap would
        # rewrite the user's discharge@40 byte to discharge-to-15.
        winners = getattr(self, "_last_user_winners", None)
        if winners is not None:
            guard_interval = self._config_int(
                CONF_SOC_GUARD_INTERVAL, DEFAULT_SOC_GUARD_INTERVAL
            ) or 120
            window_end = min(now_slot + max(guard_interval, 15) // 15, 96)
            for s in range(now_slot, window_end):
                w = winners[s] if s < len(winners) else None
                if w is not None and w.action == "discharge" and w.soc_target is not None:
                    marker = max(marker, w.soc_target)
        return marker

    @callback
    def _soc_guard_callback(self, now: datetime) -> None:
        """Periodic SoC guard timer — recompute and push updated marker."""
        if self._last_result is None or self._last_sent_slots is None:
            return
        self.hass.async_create_task(self._push_guard_update())

    async def _push_guard_update(self) -> None:
        """Recompute the SoC guard marker and resend if changed."""
        if not self._emaldo_control_enabled:
            return
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
            guard_data: dict[str, Any] = {
                "slots": slot_values,
                "high_marker": new_marker,
                "low_marker": int(cfg.soc_min),
            }
            if self._emaldo_device_id is None:
                self.resolve_emaldo_device()
            if self._emaldo_device_id:
                guard_data["device_id"] = self._emaldo_device_id
            await self.hass.services.async_call(
                EMALDO_DOMAIN,
                "apply_bulk_schedule",
                guard_data,
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

        if not self._emaldo_control_enabled:
            return

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
        if not self._emaldo_control_enabled or self._last_result is None:
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
        if self._unsub_ha_started is not None:
            self._unsub_ha_started()
            self._unsub_ha_started = None
        if self._unsub_startup is not None:
            self._unsub_startup()
            self._unsub_startup = None

    async def _async_update_data(self) -> dict[str, Any]:
        """DataUpdateCoordinator callback — returns current state."""
        return {
            "result": self._last_result,
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "reason": self._last_reason,
            "activated_time": self._activated_time,
        }
