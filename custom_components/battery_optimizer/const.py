"""Constants for the Battery Optimizer integration."""

DOMAIN = "battery_optimizer"

# ── Config entry keys ────────────────────────────────────────────────
CONF_SPOT_SENSOR = "spot_price_sensor"
CONF_SOLCAST_TODAY = "solcast_today_sensor"
CONF_SOLCAST_TOMORROW = "solcast_tomorrow_sensor"

CONF_VAT_MULTIPLIER = "vat_multiplier"
CONF_CURRENCY = "currency"
CONF_TRANSFER_FEE_BUY = "transfer_fee_buy"
CONF_SALES_COMMISSION = "sales_commission"

CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
CONF_MAX_CHARGE_KW = "max_charge_kw"
CONF_MAX_DISCHARGE_KW = "max_discharge_kw"
CONF_CHARGE_EFFICIENCY = "charge_efficiency"
CONF_DISCHARGE_EFFICIENCY = "discharge_efficiency"
CONF_SOC_MIN = "soc_min"
CONF_SOC_MAX = "soc_max"

# Issue #42: Guaranteed Grid Rewards contracts reserve capacity and give the
# Battery Range slider its own scale, so the marker is a separate setting.
CONF_BATTERY_RANGE_LOW_MARKER = "battery_range_low_marker"
CONF_RESERVED_BOTTOM_KWH = "reserved_bottom_kwh"
CONF_RESERVED_TOP_KWH = "reserved_top_kwh"

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
CONF_SOLAR_FORECAST_SOURCE = "solar_forecast_source"
CONF_SOLAR_FORECAST_SCALE = "solar_forecast_scale"
CONF_GRID_CHARGE_SOLAR_AWARE = "grid_charge_solar_aware"
CONF_SOLAR_ACTUAL_SENSOR = "solar_actual_sensor"
CONF_ENABLE_PV_CUT_WHEN_FULL = "enable_pv_cut_when_full"
CONF_PV_CUT_RESUME_SOC = "pv_cut_resume_soc"

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
CONF_PRECHARGE_PUBLISH_HOUR = "precharge_publish_hour"

# ── Solar forecast mode options ──────────────────────────────────────
SOLAR_FORECAST_P50 = "p50"  # Solcast median (optimistic, current legacy)
SOLAR_FORECAST_P10 = "p10"  # Solcast 10th-percentile (pessimistic, weather-aware)

# PV forecast source.  "solcast" (default, unchanged behaviour): an external
# sensor whose `detailedForecast` attribute carries Solcast-shaped slots.
# "emaldo": the Emaldo unit's own per-slot solar forecast, read from the
# `solar` field (watts) of its schedule_chart sensor — no forecast integration,
# no third-party account.  See `emaldo_solar.py`.
SOLAR_SOURCE_SOLCAST = "solcast"
SOLAR_SOURCE_EMALDO = "emaldo"

# ── Defaults ─────────────────────────────────────────────────────────
# ── Locale defaults ──────────────────────────────────────────────────
# Both tables below are bootstrapping defaults, not authoritative data. Every
# value is overridable by stored config, and every value should be verified by
# the user against their own contract. Tables are kept as literals rather than
# derived from an exchange rate so that a reviewer can audit them in one diff.

# Monetary defaults tuned per market, in that market's own currency.
_CURRENCY_MONETARY_DEFAULTS = {
    "EUR": {
        "battery_wear_cost": 0.03,
        "precharge_price_ceiling": 0.07,
        "forced_sell_min_profit": 0.02,
    },
    "SEK": {
        "battery_wear_cost": 0.34,
        "precharge_price_ceiling": 0.80,
        "forced_sell_min_profit": 0.23,
    },
    "NOK": {
        "battery_wear_cost": 0.35,
        "precharge_price_ceiling": 0.81,
        "forced_sell_min_profit": 0.23,
    },
    "DKK": {
        "battery_wear_cost": 0.22,
        "precharge_price_ceiling": 0.52,
        "forced_sell_min_profit": 0.15,
    },
}

# Standard VAT multiplier by ISO 3166-1 alpha-2 country (Nord Pool markets).
# Electricity is taxed at the standard rate in every listed market.
# Rates as of 2026-10; EU members checked against the Wikipedia "European Union
# value added tax" table (edited 2026-10-22), Norway (25 %, normal rate 2026)
# against skatteetaten.no. Northern Norway (Finnmark, Nord-Troms) is exempt
# from VAT on electricity; users there should set the multiplier to 1.0.
_COUNTRY_VAT_MULTIPLIER = {
    "FI": 1.255,   # 25.5% (raised from 24% on 2024-09-01)
    "SE": 1.25,
    "NO": 1.25,
    "DK": 1.25,
    "EE": 1.24,    # raised from 22% on 2025-07-01
    "LV": 1.21,
    "LT": 1.21,
    "PL": 1.23,
    "DE": 1.19,
    "NL": 1.21,
    "BE": 1.21,
    "FR": 1.20,
    "AT": 1.20,
}
DEFAULT_VAT_MULTIPLIER = _COUNTRY_VAT_MULTIPLIER["FI"]   # 25.5% Finnish electricity VAT
DEFAULT_TRANSFER_FEE_BUY = 0.0776    # €/kWh transfer + tax, VAT-inclusive
DEFAULT_SALES_COMMISSION = 0.003     # €/kWh retailer commission on feed-in
DEFAULT_BATTERY_CAPACITY_KWH = 15.0
# Inverter MAXIMUM CAPABILITY in kW, not a target or a requested setpoint.
# Emaldo's inverter API exposes no other power control, so forced selling
# always runs at this power (~10 kW is typical for Emaldo inverters).
DEFAULT_MAX_CHARGE_KW = 10.0
DEFAULT_MAX_DISCHARGE_KW = 10.0
DEFAULT_CHARGE_EFFICIENCY = 0.9
DEFAULT_DISCHARGE_EFFICIENCY = 0.9
DEFAULT_SOC_MIN = 20
DEFAULT_SOC_MAX = 100
DEFAULT_BASE_LOAD_KW = 1.0

DEFAULT_BATTERY_WEAR_COST = _CURRENCY_MONETARY_DEFAULTS["EUR"]["battery_wear_cost"]  # €/kWh cycled
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
DEFAULT_SOLAR_FORECAST_SOURCE = SOLAR_SOURCE_SOLCAST  # existing installs keep Solcast
DEFAULT_GRID_CHARGE_SOLAR_AWARE = 0.0  # 0.0 = legacy sizing (issue #26 off)

# ── Speculative grid pre-charge defaults ───────────────────────────
DEFAULT_PRECHARGE_ENABLED = False           # opt-in feature
DEFAULT_PRECHARGE_SAFETY_SOC = 0.70          # fill to 70% of usable band
DEFAULT_PRECHARGE_PRICE_CEILING = _CURRENCY_MONETARY_DEFAULTS["EUR"]["precharge_price_ceiling"]  # effective buy ceiling
DEFAULT_PRECHARGE_MAX_KWH_FRAC = 0.40        # cap speculative energy = 40% of band
DEFAULT_PRECHARGE_LOW_SOLAR_FRAC = 0.25      # P10 solar < 25% of band ⇒ "bad solar"
DEFAULT_PRECHARGE_REQUIRE_LOW_SOLAR = True   # gate on solar signal
DEFAULT_PRECHARGE_HORIZON_DAYS = 1           # 1 = pre-publish only; 2 = also day+2
DEFAULT_PRECHARGE_PUBLISH_HOUR = None        # None = auto (Nord Pool publish converted to HA timezone)

# ── Cheap-price opportunistic charge (issue #31) ─────────────────────
# Before the next-day prices are published the plan ends at midnight and
# stored energy has no value past it, so the optimizer buys nothing for an
# unknown tomorrow even at a zero price.  Opt-in: charge today's nearly
# free slots into the headroom today's own solar would not fill anyway.
CONF_CHEAP_CHARGE_ENABLED = "cheap_charge_enabled"
CONF_CHEAP_CHARGE_PRICE_THRESHOLD = "cheap_charge_price_threshold"
CONF_CHEAP_CHARGE_ONLY_WHEN_TOMORROW_UNKNOWN = "cheap_charge_only_when_tomorrow_unknown"
DEFAULT_CHEAP_CHARGE_ENABLED = False
DEFAULT_CHEAP_CHARGE_THRESHOLD_FRACTION = 0.5   # of the locale's pre-charge price ceiling
DEFAULT_CHEAP_CHARGE_ONLY_WHEN_TOMORROW_UNKNOWN = True
CHEAP_CHARGE_MIN_NEED_KWH = 0.05                # below this headroom, do nothing

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

# ── Stale-stream push retry (issue #24) ──────────────────────────────
# During a 21204 reconnect storm the Emaldo stream flaps stale<->connected
# every ~90 s while the optimizer only samples it once per cycle (120 min).
# The v0.3.17 storm amplifier guard skipped the whole push on a stale
# sample, so the rolling override never covered the night. New behavior:
# push through the stale stream, retrying with a short backoff so the push
# lands in one of the connected windows. Retries are bounded and only
# delay the coordinator by backoff × retries at most.
CONF_STALE_PUSH_RETRY_COUNT = "stale_push_retry_count"
DEFAULT_STALE_PUSH_RETRY_COUNT = 3     # additional attempts after the first
STALE_PUSH_RETRY_BACKOFF_S = 30.0

# ── Override enforcement (issue #36) ─────────────────────────────────
# The Emaldo apply_bulk_schedule service returns nothing on failure, so the
# coordinator keeps its own push bookkeeping and reads the device back.
PUSH_FAIL_REASSERT_WINDOW_S = 600     # re-push an unchanged plan after a recent failure
PUSH_SETTLE_S = 300                   # a push this fresh is not judged yet
ENFORCEMENT_REASSERT_MIN_S = 1800     # re-push an unchanged plan after this long
ACCURACY_REASSERT_COOLDOWN_S = 900    # min age of the last push before the
                                      # accuracy shortfall may trigger a re-push
ACCURACY_REASSERT_SHORTFALL_KWH = 0.35  # planned discharge needed to judge
ACCURACY_REASSERT_RATIO = 0.25        # actual < planned × ratio ⇒ shortfall
ENFORCEMENT_CHECK_SETTLE_S = 120      # do not read the device back sooner after a push
ENFORCEMENT_REPUSH_MIN_S = 600        # min spacing between read-back re-pushes

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

# ── Forced sell (manual sell arbitrage) ─────────────────────────────
CONF_FORCED_SELL_ENABLED = "forced_sell_enabled"
CONF_FORCED_SELL_MAX_KWH_PD = "forced_sell_max_kwh_pd"
CONF_FORCED_SELL_MIN_PROFIT = "forced_sell_min_profit"
CONF_FORCED_SELL_MIN_PRICE = "forced_sell_min_price"
DEFAULT_FORCED_SELL_ENABLED = False
DEFAULT_FORCED_SELL_MAX_KWH_PD = 0.0   # 0 = unlimited
DEFAULT_FORCED_SELL_MIN_PROFIT = _CURRENCY_MONETARY_DEFAULTS["EUR"]["forced_sell_min_profit"]  # after wear + round-trip
DEFAULT_FORCED_SELL_MIN_PRICE = 0.0    # absolute sell-price floor (€/kWh)
DEFAULT_ENABLE_PV_CUT_WHEN_FULL = False
DEFAULT_PV_CUT_RESUME_SOC = 97.0       # percent; resume third-party PV below this

# ── Emaldo slot encoding (mirrors emaldo_lib.const) ─────────────────
SLOT_NO_OVERRIDE = 0x80  # 128 — follow base schedule
SLOT_IDLE = 0x00
EMALDO_DOMAIN = "emaldo"

# ── Timing ───────────────────────────────────────────────────────────
SLOTS_PER_DAY = 96
SLOT_DURATION_HOURS = 0.25  # 15 minutes

# Fixed midnight checkpoint (always runs regardless of interval)
MIDNIGHT_CHECKPOINT = (0, 1)

# Last-resort pre-publish cutoff (slot index from local midnight).  Used only
# when the HA timezone AND the system local timezone are both unavailable —
# the publish instant is normally derived from NORD_POOL_PUBLISH_TZ/HOUR
# (DST-correct, any market).  The hardcoded value fits one market; it is not
# used unless both timezone lookups fail.
PUBLISH_CUTOFF_SLOT = 56

# Nord Pool publishes next-day prices at ~13:00 in its operational (CET/CEST)
# timezone, year-round. Converting that instant to the HA timezone yields the
# correct pre-publish cutoff slot for any market and season (all Nord Pool
# markets shift DST together — a fixed UTC hour would drift by 1h in summer).
NORD_POOL_PUBLISH_TZ = "Europe/Stockholm"
NORD_POOL_PUBLISH_HOUR = 13

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
    (the remaining Nord Pool markets trade in euro).
    """
    return _TZ_CURRENCY.get(tz_name, "EUR")
