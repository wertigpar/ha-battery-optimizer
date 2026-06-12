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

DEFAULT_SOLAR_POWER_SENSOR = ""
DEFAULT_GRID_POWER_SENSOR = ""
DEFAULT_BATTERY_POWER_SENSOR = ""

DEFAULT_AUTO_BASE_LOAD = False
DEFAULT_LOAD_ENERGY_SENSOR = ""
DEFAULT_ENABLE_PV_STRATEGY = False
DEFAULT_ENABLE_EMALDO_CONTROL = True
DEFAULT_SOLAR_SELL_MIN_FORECAST_KWH = 10.0  # kWh; below this, cloudy day → skip
DEFAULT_SOLAR_FORECAST_MODE = SOLAR_FORECAST_P10  # conservative planning by default

# ── Optimizer run interval ────────────────────────────────────────────
DEFAULT_OPTIMIZER_INTERVAL = 120   # minutes
OPTIMIZER_INTERVALS = [15, 30, 60, 120]

# ── SoC Guard ────────────────────────────────────────────────────────
DEFAULT_SOC_GUARD_INTERVAL = 0   # minutes, 0 = disabled
SOC_GUARD_INTERVALS = [0, 15, 30, 60, 120]

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
