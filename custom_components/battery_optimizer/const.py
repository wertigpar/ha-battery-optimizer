"""Constants for the Battery Optimizer integration."""

DOMAIN = "battery_optimizer"

# ── Config entry keys ────────────────────────────────────────────────
CONF_SPOT_SENSOR = "spot_price_sensor"
CONF_SOLCAST_TODAY = "solcast_today_sensor"
CONF_SOLCAST_TOMORROW = "solcast_tomorrow_sensor"

CONF_VAT_MULTIPLIER = "vat_multiplier"
CONF_TRANSFER_FEE_BUY = "transfer_fee_buy"
CONF_SALES_COMMISSION = "sales_commission"

CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
CONF_MAX_CHARGE_KW = "max_charge_kw"
CONF_MAX_DISCHARGE_KW = "max_discharge_kw"
CONF_CHARGE_EFFICIENCY = "charge_efficiency"
CONF_DISCHARGE_EFFICIENCY = "discharge_efficiency"
CONF_SOC_MIN = "soc_min"
CONF_SOC_MAX = "soc_max"

CONF_BASE_LOAD_KW = "base_load_kw"

CONF_BATTERY_WEAR_COST = "battery_wear_cost"
CONF_IDLE_POWER_KW = "idle_power_kw"

CONF_ENABLE_SOC_SAFEGUARD = "enable_soc_safeguard"
CONF_SOC_RECOVERY_BUFFER = "soc_recovery_buffer_pct"

CONF_IDLE_STRATEGY = "idle_strategy"

CONF_PRICE_SOURCE = "price_source"
PRICE_SOURCE_EMALDO = "emaldo"
PRICE_SOURCE_SENSOR = "sensor"
DEFAULT_PRICE_SOURCE = PRICE_SOURCE_EMALDO

CONF_SOC_GUARD_INTERVAL = "soc_guard_interval"
CONF_OPTIMIZER_INTERVAL = "optimizer_interval"
CONF_EMALDO_ENTRY_ID = "emaldo_entry_id"
CONF_AUTO_BASE_LOAD = "auto_base_load"
CONF_LOAD_ENERGY_SENSOR = "load_energy_sensor"
CONF_ENABLE_PV_STRATEGY = "enable_pv_strategy"
CONF_SOLAR_SELL_MIN_FORECAST_KWH = "solar_sell_min_forecast_kwh"
CONF_ENABLE_EMALDO_CONTROL = "enable_emaldo_control"
CONF_SOLAR_FORECAST_MODE = "solar_forecast_mode"
CONF_SOLAR_FORECAST_SCALE = "solar_forecast_scale"
CONF_SOLAR_ACTUAL_SENSOR = "solar_actual_sensor"

CONF_GRID_IMPORT_SENSOR = "grid_import_sensor"
CONF_GRID_EXPORT_SENSOR = "grid_export_sensor"
CONF_RULE_RETENTION_DAYS = "rule_retention_days"

# ── Speculative grid pre-charge (low-price / bad-solar safety fill) ──
CONF_PRECHARGE_ENABLED = "precharge_enabled"
CONF_PRECHARGE_SAFETY_SOC = "precharge_safety_soc"
CONF_PRECHARGE_PRICE_CEILING = "precharge_price_ceiling"
CONF_PRECHARGE_MAX_KWH_FRAC = "precharge_max_kwh_frac"
CONF_PRECHARGE_LOW_SOLAR_FRAC = "precharge_low_solar_frac"
CONF_PRECHARGE_REQUIRE_LOW_SOLAR = "precharge_require_low_solar"
CONF_PRECHARGE_HORIZON_DAYS = "precharge_horizon_days"

# ── Solar forecast mode options ──────────────────────────────────────
SOLAR_FORECAST_P50 = "p50"  # Solcast median (optimistic, current legacy)
SOLAR_FORECAST_P10 = "p10"  # Solcast 10th-percentile (pessimistic, weather-aware)

# ── Defaults ─────────────────────────────────────────────────────────
DEFAULT_VAT_MULTIPLIER = 1.255       # 25.5% Finnish electricity VAT
DEFAULT_TRANSFER_FEE_BUY = 0.0776    # €/kWh transfer + tax
DEFAULT_SALES_COMMISSION = 0.003     # €/kWh retailer commission on feed-in
DEFAULT_BATTERY_CAPACITY_KWH = 15.0
DEFAULT_MAX_CHARGE_KW = 10.0
DEFAULT_MAX_DISCHARGE_KW = 10.0
DEFAULT_CHARGE_EFFICIENCY = 0.9
DEFAULT_DISCHARGE_EFFICIENCY = 0.9
DEFAULT_SOC_MIN = 20
DEFAULT_SOC_MAX = 100
DEFAULT_BASE_LOAD_KW = 1.0

DEFAULT_BATTERY_WEAR_COST = 0.03           # €/kWh cycled (3 snt/kWh)
DEFAULT_IDLE_POWER_KW = 0.1               # 100W battery unit idle consumption

# ── SoC floor safeguard ──────────────────────────────────────
DEFAULT_ENABLE_SOC_SAFEGUARD = True
DEFAULT_SOC_RECOVERY_BUFFER_PCT = 5.0  # keep-alive charge target = soc_min + buffer
# Forced re-run when actual SoC drops this close to (or below) soc_min
LOW_SOC_RERUN_MARGIN_PCT = 2.0
# Minimum minutes between low-SoC forced re-runs
LOW_SOC_RERUN_THROTTLE_MIN = 30

# L2 idle-gap replan: minimum minutes between re-runs when the plan leaves
# the current slot idle while the grid buys at a price above wear cost.
IDLE_GAP_RERUN_THROTTLE_MIN = 30
# L3 divergence replan: actual SoC deviating this much from the plan's
# projected slot SoC triggers an event-driven re-run (throttled).
SOC_DIVERGENCE_RERUN_THRESHOLD_PCT = 5.0
SOC_DIVERGENCE_RERUN_THROTTLE_MIN = 15

DEFAULT_AUTO_BASE_LOAD = False
DEFAULT_LOAD_ENERGY_SENSOR = ""
DEFAULT_ENABLE_PV_STRATEGY = False
DEFAULT_ENABLE_EMALDO_CONTROL = True
DEFAULT_SOLAR_SELL_MIN_FORECAST_KWH = 10.0  # kWh; below this, cloudy day → skip
DEFAULT_SOLAR_FORECAST_MODE = SOLAR_FORECAST_P10  # conservative planning by default

# ── Speculative grid pre-charge defaults ───────────────────────────
DEFAULT_PRECHARGE_ENABLED = False           # opt-in feature
DEFAULT_PRECHARGE_SAFETY_SOC = 0.70          # fill to 70% of usable band
DEFAULT_PRECHARGE_PRICE_CEILING = 0.07       # €/kWh effective buy ceiling
DEFAULT_PRECHARGE_MAX_KWH_FRAC = 0.40        # cap speculative energy = 40% of band
DEFAULT_PRECHARGE_LOW_SOLAR_FRAC = 0.25      # P10 solar < 25% of band ⇒ "bad solar"
DEFAULT_PRECHARGE_REQUIRE_LOW_SOLAR = True   # gate on solar signal
DEFAULT_PRECHARGE_HORIZON_DAYS = 1           # 1 = pre-publish only; 2 = also day+2

# ── Solar forecast scale (over-forecast compensation) ────────────────
DEFAULT_SOLAR_FORECAST_SCALE = 0.0   # 0.0 = auto-tune sentinel (0 = auto)
DEFAULT_SOLAR_ACTUAL_SENSOR = ""      # empty = Emaldo-internal estimate
# Realized cost history — metered import/export energy counters. Defaults to
# the Emaldo cloud daily counters; override with lifetime grid meters
# (e.g. an EM24 energy meter) for cloud-free, reset-safe accounting.
DEFAULT_GRID_IMPORT_SENSOR = ""
DEFAULT_GRID_EXPORT_SENSOR = ""
SOLAR_SCALE_MIN = 0.3                # effective scale floor (also manual clamp floor)
SOLAR_SCALE_MAX = 1.2                # effective scale ceiling
SOLAR_SCALE_EWMA_ALPHA = 0.1         # EWMA smoothing factor
SOLAR_SCALE_WARMUP_RECORDS = 5       # min valid records before auto-tune engages
SOLAR_SCALE_MIN_ELAPSED_SLOTS = 8    # min accuracy window (slots) for a valid record
SOLAR_SCALE_MIN_PLANNED_KWH = 0.1    # min planned solar (kWh) for a valid record

# ── Solar regime (durable no-refill discharge gate) ─────────────────
# One EWMA update per day over the scaled-forecast solar fraction of the
# usable band (all values relative to the user's own capacity — generic).
# Engaged = stored kWh must beat the cheapest known future recharge to be
# discharged (Case A floor), i.e. the battery cannot refill from solar for
# days/weeks (winter, snow).  Transient cloudy days never engage: τ≈10 days
# plus a 3-day debounce absorbs them.
SOLAR_REGIME_EWMA_ALPHA = 0.1      # per day; τ ≈ 10 days
SOLAR_REGIME_ENGAGE = 0.25         # EWMA below this → low-regime counting
SOLAR_REGIME_DISENGAGE = 0.40      # EWMA above this → normal-regime counting
SOLAR_REGIME_DEBOUNCE_DAYS = 3     # consecutive days on one side before flip

# ── Optimizer run interval ────────────────────────────────────────────
DEFAULT_OPTIMIZER_INTERVAL = 120   # minutes
OPTIMIZER_INTERVALS = [15, 30, 60, 120]

# ── SoC Guard ────────────────────────────────────────────────────────
DEFAULT_SOC_GUARD_INTERVAL = 0   # minutes, 0 = disabled
SOC_GUARD_INTERVALS = [0, 15, 30, 60, 120]

# ── Date-rule retention (auto-delete expired date rules) ─────────────
DEFAULT_RULE_RETENTION_DAYS = 0   # 0 = keep forever (feature off)
RULE_RETENTION_OPTIONS = [0, 1, 7, 30]

# ── Idle strategy options ────────────────────────────────────────────
IDLE_FULL_CONTROL = "full_control"
IDLE_SOLAR_GUARD = "solar_guard"
IDLE_SMART_OVERRIDE = "smart_override"
DEFAULT_IDLE_STRATEGY = IDLE_FULL_CONTROL

# ── Emaldo slot encoding (mirrors emaldo_lib.const) ─────────────────
SLOT_NO_OVERRIDE = 0x80  # 128 — follow base schedule
SLOT_IDLE = 0x00
EMALDO_DOMAIN = "emaldo"

# ── Timing ───────────────────────────────────────────────────────────
SLOTS_PER_DAY = 96
SLOT_DURATION_HOURS = 0.25  # 15 minutes

# Fixed midnight checkpoint (always runs regardless of interval)
MIDNIGHT_CHECKPOINT = (0, 1)

# Nordpool tomorrow-price publish ~14:00 CET → slot index (00:00 + 14h, 15min slots)
PUBLISH_CUTOFF_SLOT = 56

# ── Config subentries ────────────────────────────────────────────────
SUBENTRY_TYPE_RULE = "rule"
DEFAULT_RULE_LABEL = "Default Schedule"

SUBENTRY_TYPE_DEVICE = "device"
DEVICE_SUBENTRY_LABEL = "Battery Optimizer Configuration"
DEVICE_SUBENTRY_UNIQUE_ID = "optimizer_device"

# ── Currency (Nord Pool region, mirrors emaldo price_unit_for_timezone) ─
_TZ_CURRENCY = {
    "Europe/Stockholm": "SEK",
    "Europe/Oslo": "NOK",
    "Europe/Copenhagen": "DKK",
}


def currency_for_timezone(tz_name: str) -> str:
    """ISO 4217 currency code for a Nord Pool timezone.

    Returns SEK/NOK/DKK for the Scandinavian timezones, EUR otherwise
    (Finland and the rest of the Nord Pool area trade in euro).
    """
    return _TZ_CURRENCY.get(tz_name, "EUR")
