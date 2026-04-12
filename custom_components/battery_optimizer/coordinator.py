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
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
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
    CONF_BATTERY_SOC_SENSOR,
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
    CONF_BATTERY_PRICE,
    CONF_BATTERY_LIFETIME_CYCLES,
    CONF_IDLE_POWER_KW,
    CONF_IDLE_STRATEGY,
    CONF_SOC_GUARD_INTERVAL,
    CONF_OPTIMIZER_INTERVAL,
    CONF_SOLAR_POWER_SENSOR,
    DEFAULT_IDLE_STRATEGY,
    DEFAULT_SOC_GUARD_INTERVAL,
    DEFAULT_OPTIMIZER_INTERVAL,
    DEFAULT_SOLAR_POWER_SENSOR,
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
    if action == "charge":
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
        # Solar adaptation state: actual readings per 15-min slot
        self._actual_solar: list[float | None] = [None] * SLOTS_PER_DAY
        self._solar_date: datetime | None = None  # date the readings belong to

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
    def soc_guard_marker(self) -> int | None:
        """Current SoC guard high_marker, or None if guard is disabled."""
        guard_interval = self.config.get(
            CONF_SOC_GUARD_INTERVAL, DEFAULT_SOC_GUARD_INTERVAL
        )
        if guard_interval <= 0:
            return None
        return self._current_guard_marker

    def _build_battery_config(self) -> BatteryConfig:
        """Create a BatteryConfig from the current HA config."""
        c = self.config
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
            base_load_kw=c.get(CONF_BASE_LOAD_KW, 0.5),
            battery_price=c.get(CONF_BATTERY_PRICE, 9000.0),
            battery_lifetime_cycles=c.get(CONF_BATTERY_LIFETIME_CYCLES, 10000),
            idle_power_kw=c.get(CONF_IDLE_POWER_KW, 0.1),
        )

    # ── Data readers ──────────────────────────────────────────────────

    def _parse_price_data(
        self,
    ) -> tuple[list[float] | None, list[float] | None]:
        """Parse the sensor 'data' attribute into today/tomorrow 96-slot prices.

        The sensor stores a flat list of {start, end, price} dicts at 15-min
        resolution, with prices in snt/kWh.  We split by date, convert to
        €/kWh, and return (today_96, tomorrow_96).  Either may be None if
        insufficient data exists.
        """
        sensor_id = self.config[CONF_SPOT_SENSOR]
        state = self.hass.states.get(sensor_id)
        if state is None:
            _LOGGER.warning("Price sensor %s not found", sensor_id)
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
        sensor_id = self.config[CONF_SPOT_SENSOR]
        state = self.hass.states.get(sensor_id)
        if state is None:
            return False
        if state.attributes.get("tomorrow_valid", False):
            return True
        # Fallback: parse and check
        _, tomorrow = self._parse_price_data()
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

        slots_30min = [s.get("pv_estimate", 0.0) for s in detailed]
        return interpolate_solar_to_15min(slots_30min)

    def _get_battery_soc(self) -> float | None:
        """Read current battery SoC from sensor."""
        sensor_id = self.config[CONF_BATTERY_SOC_SENSOR]
        state = self.hass.states.get(sensor_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    # ── Solar forecast adaptation ─────────────────────────────────────

    def _read_solar_power_kw(self) -> float | None:
        """Read current solar production from the configured power sensor.

        The sensor is expected to report watts — we convert to kW.
        Returns None if sensor is unavailable.
        """
        sensor_id = self.config.get(
            CONF_SOLAR_POWER_SENSOR, DEFAULT_SOLAR_POWER_SENSOR
        )
        state = self.hass.states.get(sensor_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        # Convert watts to kW (handle sensors that already report kW)
        unit = str(state.attributes.get("unit_of_measurement", "")).lower()
        if "kw" in unit:
            return max(value, 0.0)
        return max(value / 1000.0, 0.0)

    def _record_current_solar(self) -> None:
        """Record the current solar power reading for the current slot.

        Resets all readings at the start of a new day.
        """
        today = dt_util.now().date()
        if self._solar_date != today:
            self._actual_solar = [None] * SLOTS_PER_DAY
            self._solar_date = today

        reading = self._read_solar_power_kw()
        if reading is not None:
            slot = _current_slot_index()
            self._actual_solar[slot] = reading

    def _adapt_solar_forecast(self, forecast: list[float]) -> list[float]:
        """Adapt Solcast forecast using actual solar power measurements.

        For elapsed slots with actual data, replace forecast values.
        For future slots, apply a dampening factor derived from the ratio
        of actual vs forecast production over recent daylight slots.
        """
        self._record_current_solar()
        now_slot = _current_slot_index()

        adapted = list(forecast)

        # Replace past slots with actual measurements where available
        for s in range(min(now_slot + 1, SLOTS_PER_DAY)):
            if self._actual_solar[s] is not None:
                adapted[s] = self._actual_solar[s]

        # Compute dampening factor from daylight slots with both readings.
        # Only use slots where forecast predicts meaningful solar (>0.1 kW).
        forecast_sum = 0.0
        actual_sum = 0.0
        count = 0
        for s in range(min(now_slot, SLOTS_PER_DAY)):
            if forecast[s] > 0.1 and self._actual_solar[s] is not None:
                forecast_sum += forecast[s]
                actual_sum += self._actual_solar[s]
                count += 1

        if count >= 2 and forecast_sum > 0:
            ratio = actual_sum / forecast_sum
            # Clamp: don't over-correct in either direction
            dampening = max(0.3, min(1.5, ratio))
            _LOGGER.info(
                "Solar adaptation: %d slots, actual/forecast ratio=%.2f, "
                "dampening=%.2f",
                count, ratio, dampening,
            )
            for s in range(now_slot + 1, SLOTS_PER_DAY):
                adapted[s] = forecast[s] * dampening
        else:
            _LOGGER.debug(
                "Solar adaptation: insufficient data (%d daylight slots), "
                "using raw forecast",
                count,
            )

        return adapted

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

        solar = self._adapt_solar_forecast(self._get_solcast_forecast("today"))
        soc = self._get_battery_soc()
        cfg = self._build_battery_config()
        now_slot = _current_slot_index()

        if soc is None:
            _LOGGER.error(
                "Cannot optimize: battery SoC sensor '%s' returned None. "
                "Check that the sensor exists and is not 'unknown'/'unavailable'.",
                self.config.get(CONF_BATTERY_SOC_SENSOR, "(not configured)"),
            )
            return None

        _LOGGER.info("Battery SoC: %.1f%%, start_slot: %d", soc, now_slot)

        if not force and self._last_result is not None:
            if not self._should_reoptimize(soc, cfg):
                _LOGGER.info("Skipping optimization — no significant changes")
                return self._last_result

        # Run optimizer — prices_today is already 96 x 15-min in €/kWh
        buy_prices, sell_prices = compute_prices(prices_today, cfg)
        result = optimize(
            buy_prices,
            sell_prices,
            solar,
            cfg,
            start_slot=now_slot,
            initial_soc_pct=soc,
        )
        result.reason = reason

        self._last_result = result
        self._last_run = dt_util.now()
        self._last_reason = reason

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
        if not self.hass.services.has_service(EMALDO_DOMAIN, "apply_bulk_schedule"):
            _LOGGER.warning(
                "Emaldo service 'apply_bulk_schedule' not available — "
                "schedule computed but not applied"
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
            # Smart diff against internal AI schedule (today = first 96)
            # Skip smart diff for idle overrides in full_control mode —
            # the AI can change its mind after we read it, so we must
            # always send the explicit SLOT_IDLE byte.
            if sp.action != "idle" or idle_strategy != IDLE_FULL_CONTROL:
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
                # Smart diff against internal schedule (tomorrow = offset 96)
                # Same full_control guard as today's loop.
                if sp.action != "idle" or idle_strategy != IDLE_FULL_CONTROL:
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

        for entry_data in emaldo_data.values():
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
        for entry_data in emaldo_data.values():
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

        # 3) Nordpool state change — re-run when tomorrow's prices arrive
        spot_sensor = self.config[CONF_SPOT_SENSOR]
        unsub = async_track_state_change_event(
            self.hass,
            [spot_sensor],
            self._nordpool_state_change,
        )
        self._unsub_listeners.append(unsub)

        _LOGGER.info(
            "Listeners set up: midnight checkpoint + %d-min interval + Nordpool watcher on %s",
            opt_interval, spot_sensor,
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

    @callback
    def async_shutdown(self) -> None:
        """Clean up listeners."""
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
