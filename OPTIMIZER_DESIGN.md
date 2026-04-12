# Battery Optimizer — Design & Parameters

## Optimization Targets

The optimizer produces a 96-slot (15-minute) schedule per day. When tomorrow's prices are available, it also produces a separate 96-slot plan for tomorrow. Both are pushed to the battery via a single rolling 96-slot E2E override window. Its main goals, in priority order:

- **Maximize savings from self-consumption** — the Emaldo battery load-matches during discharge (covers household load only, no grid export). Discharge value equals the grid buy price avoided, not the sell/export price. A discharge slot is scheduled when `buy_price > wear_cost`.
- **Maximize free solar self-consumption** — when solar production exceeds household load, capture surplus energy in the battery at zero cost instead of exporting it at a low feed-in price.
- **Round-trip arbitrage** — buy cheap grid energy, store it, discharge later to avoid expensive grid purchases. Only worthwhile when `buy_saved > buy_charged / (η_c × η_d) + wear_cost`.
- **Avoid unnecessary grid charging** — if the battery is already full (e.g. from solar), do not schedule grid charging. Grid charge is limited to the deficit that solar and existing SoC cannot cover.
- **Respect SoC constraints** — never charge above `soc_max` or discharge below `soc_min`.
- **Account for round-trip efficiency losses** — both charge and discharge efficiency are factored into round-trip profitability checks.
- **Account for battery wear cost** — `wear_cost = battery_price / (lifetime_cycles × capacity_kwh)` is the full round-trip degradation cost per kWh (e.g. 9000 / 10000 / 15 = 0.06 €/kWh). This is a flat cost — no efficiency division needed since it already represents the full cycle.
- **Account for battery idle power** — the battery unit draws constant power (default 0.1 kW / 100 W) which drains SoC in every slot. This is subtracted from all SoC projections, solar budget estimates, and discharge energy budgets. At 0.1 kW this equals 2.4 kWh/day (~16 % of 15 kWh).
- **Discharge for self-consumption first** — existing stored energy (from solar or prior charging) is discharged when the avoided grid buy price exceeds the wear cost. No need to compare against sell/export price since the battery never exports during discharge.
- **Prefer discharge at highest buy-price slots first** — greedy assignment from most expensive grid buy price downward (biggest savings first). Discharge candidates are non-solar slots only.
- **Prefer charge at cheapest buy-price slots first** — greedy assignment from cheapest buy price upward.
- **Plan today + tomorrow** — when next-day prices are available (typically after 14:00), the optimizer also plans all 96 slots for tomorrow. Both plans are pushed via a rolling 96-slot E2E window: positions `[now_slot..95]` carry today's plan, positions `[0..now_slot-1]` carry tomorrow's plan.
- **Re-optimize on schedule and events** — fixed midnight checkpoint at 00:01 plus configurable periodic re-runs (15/30/60/120 min, default 120) and immediate re-run when Nordpool publishes new prices. Conditional runs skip if SoC deviation < 10%.

## Algorithm Overview (Greedy)

1. **Classify slots** — solar surplus (net_load < 0) vs. grid slots (net_load ≥ 0)
2. **Find Case A discharge candidates** — existing stored energy can be discharged for self-consumption when `buy_price > wear_cost`. Discharge candidates are grid slots only (sorted by buy price descending). During solar surplus the battery has zero self-consumption value.
3. **Find Case B round-trip pairs** — grid charge is added when `buy_saved > buy_charged / round_trip + wear_cost` (buy cheap now, discharge later to avoid expensive grid purchases)
4. **Assign discharge** (highest buy price first), then **solar idle** (free energy), then **grid charge** (deficit only). Discharge energy per slot = `min(net_load, max_discharge) × slot_duration` (load-matched, not full-rate)
5. **Simulate SoC** through all 96 slots and build the Emaldo byte schedule

## Emaldo Slot Encoding & Battery Behaviour

| Byte Value | Meaning | Grid Draw | Solar Charge |
|---|---|---|---|
| **0** (IDLE) | Force idle — no grid interaction | **No** | **Yes** — absorbs excess solar, exports only when full |
| **1–100** | Charge to N% SoC from any source | **Yes** | Yes |
| **128** | No override — follow built-in AI schedule | AI decides | AI decides |
| **129–255** | Discharge to (256 − N)% SoC | **No** — load-matched, covers household load only | N/A |

**Key insights**:
- IDLE (0x00) is effectively "solar-only charge" — the battery absorbs free solar surplus without drawing from the grid. This makes IDLE the correct command for solar surplus slots.
- Discharge (129–255) is **load-matched** — the battery automatically adjusts its discharge rate to match household load. It does not export to grid during discharge. There is no API to control this behavior; it is handled by the battery hardware.

## What the Optimizer Does NOT Currently Do

- **No multi-day lookahead** — each day is optimized independently; no "save energy for tomorrow's peak"
- **No dynamic load forecasting** — uses a single flat `base_load_kw` constant instead of actual load patterns
- **No real-time re-adjustment** — re-optimizes at checkpoints but doesn't react to unplanned spikes/drops between checkpoints
- **No grid export during discharge** — the battery load-matches and does not export. Grid export limits are therefore not relevant for discharge
- **No time-of-use tariff structures** — only spot market prices (no off-peak/peak tiers)
- **No battery temperature awareness** — doesn't throttle charge/discharge based on BMS temperature
- **No State of Health (SoH) tracking** — wear cost uses a fixed linear model, doesn't read actual cycle count or SoH from the battery

---

## Available Parameters & Data Sources

### A. Electricity Price Data

| # | Parameter | Used in Optimizer | Notes |
|---|---|---|---|
| 1 | **Spot price (15-min data attribute)** | **YES** — primary input. `data` attribute parsed into 96 buy/sell prices per day. | Configured via `spot_price_sensor`. Template sensor with 15-min resolution `data` list |
| 2 | Nord Pool current price | No — optimizer reads the `data` attribute from the configured spot price sensor | Official HA Nord Pool integration, hourly |
| 3 | Highest / lowest price today | No | Could be useful for pre-filtering — if highest price < min profitable sell, skip optimization entirely |
| 4 | Average price today | No | Could be useful as a benchmark — if today's average is very low, profit potential is minimal |
| 5 | Average price 7 days | No | Useful context: weekly average shows price trend. Could inform wear cost threshold tuning |

### B. Solar / PV Data

| # | Parameter | Used in Optimizer | Notes |
|---|---|---|---|
| 6 | **Solcast today forecast** | **YES** — `detailedForecast` attribute (48 × 30-min slots) interpolated to 96 × 15-min | Configured via `solcast_today_sensor`. Primary solar input |
| 7 | **Solcast tomorrow forecast** | **YES** — same format, used for tomorrow optimization | Configured via `solcast_tomorrow_sensor` |
| 8 | Solcast power now | No | Real-time estimate. Could be compared to actual solar to detect forecast accuracy drift |
| 9 | Solcast remaining today | No | Quick check: if remaining solar > battery headroom, discharge before solar arrives |
| 10 | **Solar inverter AC power (actual)** | **YES** — configured as `solar_power_sensor`. Used for solar forecast adaptation (dampening factor) | Live actual solar production. Compared vs Solcast forecast to detect deviation and adjust future slot forecasts |
| 11 | Solar generation today | No | **Potential value:** compare actual generation vs Solcast forecast to calculate daily forecast accuracy |
| 12 | Inverter telemetry (voltage, current, temperature) | No | Useful for monitoring but not optimization |

### C. Battery Data (Emaldo / Power Store)

| # | Parameter | Used in Optimizer | Notes |
|---|---|---|---|
| 13 | **Battery SoC** | **YES** — `initial_soc_pct` for optimization, used to calculate starting energy | Configured via `battery_soc_sensor`. Primary battery input |
| 14 | Battery capacity (live) | No — capacity is a **config parameter**, not read from the live sensor | **Potential value:** read actual capacity from sensor instead of config default — could auto-calibrate |
| 15 | Battery power | **YES (config)** — configured as `battery_power_sensor` for dashboard, but NOT used in optimization | **Potential value:** detect if battery is actually executing the planned action. If idle when it should be discharging, something is wrong |
| 16 | Grid power (Emaldo view) | No | Emaldo's view of grid power. May differ from the grid meter |
| 17 | Load power | No | **Potential value:** actual household load seen by the Emaldo. History could build a real load profile per time-of-day to replace flat `base_load_kw` |
| 18 | Battery charged / discharged today | No | **Potential value:** track actual energy throughput vs planned. Verify optimizer estimates match reality. Over time, derive actual round-trip efficiency |
| 19 | Plan source | No | Confirms whether Emaldo is running our override or its internal AI schedule |
| 20 | Active mode | No | **Potential value:** verify the battery is actually doing what the optimizer told it to do. If active_mode ≠ planned action for the current slot, log a warning or trigger re-optimization |
| 21 | Schedule chart (Emaldo) | No | Emaldo's own schedule view. Low value since optimizer overrides it |

### D. Grid Meter Data

| # | Parameter | Used in Optimizer | Notes |
|---|---|---|---|
| 22 | **Grid power total** | **YES (config)** — configured as `grid_power_sensor` for dashboard, but NOT used in optimization | Negative = exporting. **Potential value:** real-time grid import/export could validate that solar surplus slots are correctly identified |
| 23 | Per-phase power / voltage / current | No | Per-phase data. Low value for optimization, useful for phase balancing monitoring |
| 24 | Grid apparent power / power factor | No | Informational only |
| 25 | Grid energy imported / exported (cumulative) | No | Long-term cumulative. Could derive daily import/export to validate optimizer's cost estimates |
| 26 | 15-min grid energy usage | No | **Potential value:** this is a 15-min accumulation — matches optimizer slot resolution exactly. Could be used for real-time plan-vs-actual comparison per slot |

### E. Battery Telemetry (E2E Protocol — available but not as HA entities)

The Emaldo integration can read detailed BMS data via E2E encrypted protocol. These are available in the integration's internal data but **not currently exposed as HA sensors**:

| # | Parameter | Available | Exposed as HA Sensor | Potential Value |
|---|---|---|---|---|
| 48 | Battery voltage (V) | Yes | No | Could detect battery health degradation (voltage sag under load) |
| 49 | Battery current (A) | Yes | No | Verify actual charge/discharge rate matches planned |
| 50 | Cycle count | Yes | No | **HIGH VALUE** — could auto-update `battery_lifetime_cycles` remaining and refine wear cost calculation based on actual cycles |
| 51 | State of Health (SoH %) | Yes | No | **HIGH VALUE** — adjusts effective capacity. A battery at 80% SoH has less usable capacity. Currently optimizer uses nominal capacity which may overestimate |
| 52 | BMS temperature (°C) | Yes | No | **MEDIUM VALUE** — cold temperatures reduce capacity and efficiency. Could adjust charge/discharge efficiency seasonally |
| 53 | Electrode A temperature (°C) | Yes | No | Battery safety monitoring |
| 54 | Electrode B temperature (°C) | Yes | No | Battery safety monitoring |
| 55 | Fault bits | Yes | No | Should disable optimization if faults detected |

---

## Parameter Impact Assessment

### High Potential Value (worth implementing)

1. **Load power history** — Replace flat `base_load_kw` with a time-of-day load profile derived from historical data. Impact: more accurate solar surplus calculation.

2. **Actual solar vs forecast** ✔️ **(IMPLEMENTED)** — Solar forecast adaptation using actual solar power sensor readings. At each optimizer run, compares actual production vs Solcast forecast for elapsed daylight slots and applies a dampening factor (0.3–1.5×) to future slots. Impact: prevents the battery from waiting for solar that won't arrive on cloudy days.

3. **Battery SoH & cycle count** (E2E telemetry) — Expose as HA sensors and feed into optimizer. Impact: more accurate wear cost and capacity calculations.

4. **Live battery capacity** — Read actual capacity from the battery sensor instead of the config parameter. Impact: more accurate usable capacity in calculations.

5. **Plan-vs-actual verification** — After each slot executes, check whether the battery actually did what was planned (active mode + battery/grid power sensors). Impact: detect communication failures, Emaldo override bugs, or inverter faults. Trigger re-optimization if actual SoC diverges.

### Medium Potential Value (useful but lower priority)

6. **7-day average price** — Provide context for whether today is a high-profit or low-profit day. Could adjust aggressiveness: if prices are generally low, raise the minimum profit threshold to save battery cycles.

7. **BMS temperature** (E2E telemetry) — Adjust efficiency assumptions in winter (cold battery = lower efficiency). Nordic climate makes this relevant.

8. **15-min grid energy** — Slot-level actual grid import/export for real-time validation and performance tracking.

9. **Solcast remaining today** — Quick sanity check at re-optimization time to decide if solar is still expected.

### Low Potential Value (monitoring only)

10. Per-phase power data, voltage/current from grid meters and inverter — useful for monitoring dashboards but not for scheduling decisions.

11. Grid apparent power, power factor — informational.

12. Emaldo3EM temperature — device health, not battery health.

---

## Current Simplifications & Assumptions

| Assumption | Reality | Impact |
|---|---|---|
| Flat household load (`base_load_kw`) | Load varies significantly by time of day (0.04 kW nighttime standby to 5+ kW cooking/EV) | Solar surplus miscalculated — especially underestimated daytime when load is also higher |
| Fixed charge/discharge efficiency | Efficiency depends on power level, SoC, and temperature | Minor — LFP batteries are fairly flat across SoC range |
| Linear battery wear model | Real degradation depends on SoC, temperature, C-rate, cycling depth | Minor for LFP at moderate cycling rates |
| Solcast forecast is accurate | Clouds cause 50-80% forecast errors on individual 30-min slots | Can lead to over-reliance on solar, leaving discharge slots un-covered |
| No grid export limits | Some grid connections have export caps | Could lead to curtailment — planned discharge revenue never materializes |
| Each day optimized independently | Storing cheap energy today for tomorrow's peak could be profitable | Lost cross-day arbitrage opportunities |

---

## Decision Logging & Outcome Analysis

### Problem Statement

Currently the optimizer is entirely ephemeral — results exist only in memory and are lost on HA restart. There is no way to answer:
- Was yesterday's optimization profitable or not?
- How accurate was the solar forecast vs actual production?
- Did the battery actually execute the planned schedule?
- Are the configured efficiency values realistic?
- Is the wear cost threshold filtering out too many (or too few) cycles?

### Architecture Overview

```
                ┌─────────────────────────┐
                │  OPTIMIZATION RUN       │
                │  (coordinator.py)       │
                └─────────┬───────────────┘
                          │
              ┌───────────▼────────────┐
              │  RUN LOG (JSON file)   │   Written at each optimization run
              │  /config/battery_      │   Contains full decision context
              │  optimizer_logs/       │   + inputs + planned schedule
              └───────────┬────────────┘
                          │
     ┌────────────────────┼─────────────────────┐
     │                    │                     │
     ▼                    ▼                     ▼
┌─────────────┐  ┌──────────────────┐  ┌──────────────────┐
│ HA History  │  │ New Sensors      │  │ Daily Summary    │
│ (existing)  │  │ (optimizer)      │  │ (midnight job)   │
│             │  │                  │  │                  │
│ · SoC       │  │ · forecast_acc   │  │ Reads HA history │
│ · battery W │  │ · plan_adherence │  │ + run logs →     │
│ · grid W    │  │ · actual_profit  │  │ writes daily     │
│ · solar W   │  │                  │  │ report JSON      │
│ · prices    │  │                  │  │                  │
└─────────────┘  └──────────────────┘  └──────────────────┘
     │                    │                     │
     └────────────────────┼─────────────────────┘
                          │
              ┌───────────▼────────────┐
              │ ANALYSIS (MCP script)  │
              │ Reads logs + history   │
              │ Produces insights      │
              └────────────────────────┘
```

### Component 1: Run Log (per optimization run)

**Storage**: JSON files in `{HA config}/battery_optimizer_logs/`
**Implementation**: Use `homeassistant.helpers.storage.Store` or direct file writes
**Retention**: Keep 30 days, auto-purge older files
**Filename**: `run_YYYY-MM-DD_HHMMSS.json`

Each file captures the **full decision context** at the moment the optimizer runs:

```json
{
  "timestamp": "2026-03-21T15:00:00+02:00",
  "reason": "checkpoint",
  "forced": false,
  "start_slot": 60,

  "inputs": {
    "initial_soc_pct": 85.0,
    "spot_prices_today": [0.0042, 0.0041, ...],
    "buy_prices": [0.0829, 0.0828, ...],
    "sell_prices": [0.0012, 0.0011, ...],
    "solar_forecast_kw": [0, 0, ..., 3.2, 4.1, ...],
    "base_load_kw": 1.0
  },

  "config": {
    "capacity_kwh": 15.0,
    "charge_efficiency": 0.9,
    "discharge_efficiency": 0.9,
    "soc_min": 20,
    "soc_max": 100,
    "vat_multiplier": 1.255,
    "transfer_fee_buy": 0.0776,
    "sales_commission": 0.003,
    "wear_cost_per_kwh": 0.06,
    "max_charge_kw": 10.0,
    "max_discharge_kw": 10.0
  },

  "thresholds": {
    "min_spread_factor": 1.2346,
    "wear_cost": 0.06,
    "case_a_threshold": "buy_price > 0.06 (wear_cost)",
    "best_buy_saved": 0.1027,
    "max_buy_for_profit": null,
    "min_buy_for_discharge": null
  },

  "result": {
    "total_profit": 0.0,
    "charge_slots": 0,
    "discharge_slots": 4,
    "idle_slots": 32,
    "slot_summary": [
      {"slot": 60, "action": "idle", "soc_after": 85.0, "buy": 0.082, "sell": 0.001},
      {"slot": 61, "action": "discharge", "soc_after": 83.3, "buy": 0.082, "sell": 0.012}
    ]
  }
}
```

### Component 2: Slot Outcome Tracking (real-time, lightweight)

**Implementation**: New method in coordinator called **once per slot** (every 15 minutes) via `async_track_time_interval` or existing checkpoint logic.

At each 15-minute boundary, record what actually happened for the **previous** slot:

| Data Point | Source | Purpose |
|---|---|---|
| Actual SoC | Battery SoC sensor | Compare to planned `soc_after` |
| Actual battery power (avg) | Battery power sensor | Verify charge/discharge happened |
| Actual solar power (avg) | Solar power sensor | Compare to Solcast forecast |
| Actual grid power (avg) | Grid power sensor | Net import/export verification |
| Actual active mode | Emaldo active mode sensor | Verify Emaldo executed the override |
| Plan source | Emaldo plan source sensor | Confirm "Override" vs "Internal" |

**Storage**: Append to a rolling in-memory ring buffer (last 192 slots = 48h). Flush to a daily JSON file at midnight.

```json
{
  "date": "2026-03-21",
  "slots": [
    {
      "slot": 0,
      "planned_action": "idle",
      "planned_soc": 85.0,
      "actual_soc": 85.0,
      "actual_battery_w": 0,
      "actual_solar_w": 0,
      "actual_grid_w": 120,
      "actual_mode": "idle",
      "plan_source": "Override",
      "forecast_solar_kw": 0.0,
      "buy_price": 0.0829,
      "sell_price": 0.0012
    },
    ...
  ]
}
```

### Component 3: New Sensor Entities

Three new sensors that HA records in history (enabling long-term statistics):

#### `sensor.battery_optimizer_forecast_accuracy`

- **State**: Solar forecast accuracy as percentage (0–100%)
- **Calculation**: At each checkpoint, compare actual Solis generation today so far vs Solcast forecast for elapsed hours
- **Formula**: `min(100, actual_generation / forecast_generation × 100)` for the day so far
- **Attributes**: `actual_kwh`, `forecast_kwh`, `confidence` (based on spread of recent days)
- **Value**: Answers "can we trust today's solar forecast?" If accuracy drops below 70%, could trigger re-optimization with reduced solar expectations

#### `sensor.battery_optimizer_plan_adherence`

- **State**: Percentage of executed slots where battery did the planned action (0–100%)
- **Calculation**: For each completed slot today, check if `power_store_active_mode` matches `planned_action`
- **Mapping**: charge → active_mode in {"Charge", "charge-low", "charge-high", "charge-100"}, discharge → "Discharge", idle → "idle"
- **Attributes**: `matched_slots`, `mismatched_slots`, `total_checked`, `mismatches` (list of slot indices)
- **Value**: Detects communication failures with Emaldo, override not applied, or battery refusing commands

#### `sensor.battery_optimizer_actual_profit`

- **State**: Estimated actual savings in € for today based on executed actions and real prices
- **Calculation**: For each completed slot:
  - If battery was charging: `cost = -buy_price × actual_charge_kwh`
  - If battery was discharging (self-consumption): `savings = buy_price × actual_discharge_kwh - wear_cost × discharge_kwh`
  - Sum over all completed slots
- **Derives actual_charge_kwh/actual_discharge_kwh** from the battery power sensor × 0.25h (averaged over slot)
- **Attributes**: `planned_profit`, `actual_profit`, `deviation_eur`, `deviation_pct`
- **Value**: The bottom line — how much did self-consumption discharge save us vs buying from grid?

### Component 4: Daily Summary Report (midnight job)

At **00:05** each day, generate a summary for the previous day combining run logs + slot outcomes + HA history:

**File**: `{HA config}/battery_optimizer_logs/daily_YYYY-MM-DD.json`

```json
{
  "date": "2026-03-21",

  "planned": {
    "total_profit_eur": 1.35,
    "charge_slots": 4,
    "discharge_slots": 4,
    "idle_slots": 88,
    "grid_charge_kwh": 10.0,
    "grid_discharge_kwh": 9.0,
    "optimization_runs": 3,
    "run_reasons": ["checkpoint", "nordpool_update", "checkpoint"]
  },

  "actual": {
    "battery_charged_kwh": 11.2,
    "battery_discharged_kwh": 8.5,
    "solar_generation_kwh": 28.5,
    "grid_import_kwh": 15.3,
    "grid_export_kwh": 18.7,
    "soc_start": 85.0,
    "soc_end": 82.0,
    "plan_adherence_pct": 94.0,
    "mismatched_slots": [23, 45]
  },

  "analysis": {
    "planned_profit_eur": 1.35,
    "actual_profit_eur": 1.18,
    "profit_deviation_pct": -12.6,
    "solar_forecast_accuracy_pct": 95.0,
    "solar_forecast_kwh": 30.0,
    "solar_actual_kwh": 28.5,
    "avg_charge_efficiency": 0.89,
    "avg_discharge_efficiency": 0.88,
    "wear_cost_total_eur": 0.51,
    "net_profit_after_wear_eur": 0.67,
    "price_spread_max_eur": 0.085,
    "price_spread_used_eur": 0.062,
    "wasted_solar_kwh": 1.5,
    "unnecessary_grid_charge_kwh": 0.0
  },

  "insights": [
    "Solar forecast was 5% optimistic — actual 28.5 kWh vs forecast 30.0 kWh",
    "2 slots deviated from plan (slots 23, 45) — battery was idle when discharge was planned",
    "Actual efficiency ~0.89 charge / 0.88 discharge — close to configured 0.90/0.90",
    "Daily price spread (max buy - min sell) was only 0.085 €/kWh — low arbitrage opportunity"
  ]
}
```

### Component 5: Analysis Process (MCP-based)

An analysis script (Python, run from VS Code or as HA automation) uses the MCP connection to pull historical data and combine it with the saved logs.

#### Step-by-step Analysis Process

**Step 1: Gather data for analysis period** (e.g. last 7 days)

```
HA MCP queries:
├── get_history(<battery_soc_sensor>, 7d)                    → actual SoC curve
├── get_history(<battery_power_sensor>, 7d)                  → actual charge/discharge power
├── get_history(<battery_active_mode_sensor>, 7d)            → what battery actually did
├── get_history(<solar_power_sensor>, 7d)                    → actual solar production
├── get_history(<grid_power_sensor>, 7d)                     → actual grid import/export
├── get_history(<spot_price_sensor>, 7d)                     → actual prices
├── get_statistics(<battery_charged_today_sensor>, 7d, period=day)
├── get_statistics(<battery_discharged_today_sensor>, 7d, period=day)
├── get_statistics(<grid_energy_import_sensor>, 7d, period=day)
└── get_statistics(<grid_energy_export_sensor>, 7d, period=day)
```

**Step 2: Load run logs for the same period**

```
Read files: battery_optimizer_logs/run_2026-03-*.json
Read files: battery_optimizer_logs/daily_2026-03-*.json
```

**Step 3: Per-day analysis**

For each day:

| Metric | How to Calculate | What It Tells Us |
|---|---|---|
| **Planned vs actual profit** | Compare `daily.planned.total_profit` with actual energy × actual prices | Were our estimates accurate? |
| **Solar forecast accuracy** | `solis_generation_today` ÷ `solcast_forecast_today` × 100 | How much to trust solar planning |
| **Load estimate accuracy** | `(grid_import + solar_generation - battery_charged) ÷ 96` vs `base_load_kw` | Is our flat load assumption hurting us? |
| **Charge efficiency** | `actual_soc_gain_kwh ÷ grid_import_to_battery_kwh` | Real vs configured η_charge |
| **Discharge efficiency** | `actual_grid_export_from_battery_kwh ÷ soc_loss_kwh` | Real vs configured η_discharge |
| **Plan adherence** | Count slots where `active_mode` matches `planned_action` ÷ total | Communication/execution issues |
| **Wear cost check** | `actual_discharged_kwh × wear_cost_per_kwh` | Total degradation cost for the day |
| **Missed opportunities** | Slots where buy price > wear cost but was idle | Overly conservative optimization |
| **Wasted cycles** | Slots where battery was charging but could have been idle (solar surplus exported) | Overly aggressive grid charging |

**Step 4: Multi-day trend analysis**

| Trend | Calculation | Actionable Outcome |
|---|---|---|
| Weekly average profit | Mean of daily actual_profit | Baseline performance |
| Forecast accuracy trend | 7-day moving average | If declining, add a Solcast correction factor |
| Efficiency degradation | Compare monthly charge/discharge efficiency | Detect battery aging |
| Optimal wear cost | Plot profit vs cycles/day — find the knee | Tune `battery_price` or `lifetime_cycles` |
| Load profile | Hourly average from history | Replace `base_load_kw` with time-of-day values |
| Price volatility | Daily max-min spread | On low-spread days, how much did we save vs idle? |

**Step 5: Generate recommendations**

Based on accumulated data, the analysis produces actionable recommendations:

```
Examples:
- "base_load_kw is set to 1.0 kW but 7-day average actual load is 0.4 kW.
   Reducing to 0.5 kW would increase solar surplus detection by 1.5 kWh/day."

- "Solar forecast was consistently 15% optimistic this week.
   Consider adding a 0.85 correction factor."

- "Actual discharge efficiency is 0.86, lower than configured 0.90.
   Updating config would prevent 2 marginal cycles/week."

- "Battery charged 2.1 kWh from grid on March 19 during solar surplus hours.
   This cost 0.17€ unnecessarily."

- "Plan adherence dropped to 75% on March 20 — 6 slots the battery was idle
   when discharge was planned. Check Emaldo communication."
```

### Implementation Plan

| Phase | Scope | Files to Change | Effort |
|---|---|---|---|
| **Phase 1** | Run logging (JSON per optimization) | `coordinator.py`, new `logger.py` | Small — ~100 lines |
| **Phase 2** | Slot-level outcome tracking (15-min readings) | `coordinator.py` (new listener) | Small — ~80 lines |
| **Phase 3** | Three new sensors | `sensor.py`, `const.py` | Medium — ~150 lines |
| **Phase 4** | Daily summary job (midnight) | `coordinator.py` or new `reporting.py` | Medium — ~200 lines |
| **Phase 5** | Analysis script (MCP-based) | New standalone `_temp_scripts/analyze_optimizer_performance.py` | Medium — ~300 lines |

### Data Flow Summary

| When | What Happens | Storage |
|---|---|---|
| **Each optimization run** | Full inputs, config, thresholds, and 96-slot plan saved | JSON file: `run_YYYY-MM-DD_HHMMSS.json` |
| **Every 15 minutes** | Read actual SoC, battery power, solar, grid, active mode for previous slot | In-memory ring buffer (192 slots) |
| **Every 15 minutes** | Update the 3 new sensors (forecast accuracy, plan adherence, actual profit) | HA history/recorder (queryable) |
| **At midnight** | Combine run logs + slot actuals + HA statistics → daily summary | JSON file: `daily_YYYY-MM-DD.json` |
| **On demand (MCP)** | Pull HA history + daily summaries → multi-day analysis with recommendations | Script output / analysis report |

### Sensor Data Requirements for Analysis

For the analysis to work, these sensors must have HA recorder history enabled (they are by default):

| Entity | Needed Resolution | Used For |
|---|---|---|
| Battery SoC sensor | Every state change (~1%/5 min) | SoC trajectory comparison |
| Battery power sensor | Every state change | Actual charge/discharge power |
| Battery active mode sensor | Every state change | Plan adherence |
| Battery plan source sensor | Every state change | Override confirmation |
| Battery charged today sensor | Total increasing (daily reset) | Daily charge energy |
| Battery discharged today sensor | Total increasing (daily reset) | Daily discharge energy |
| Solar power sensor | Every state change | Actual solar production |
| Grid power sensor | Every state change | Grid import/export |
| Spot price sensor | Hourly/15-min (via data attr) | Actual price at each slot |
| Solcast forecast today sensor | ~6h updates (via detailedForecast) | Forecast baseline |
| Grid energy import sensor | Long-term statistics | Cumulative grid import |
| Grid energy export sensor | Long-term statistics | Cumulative grid export |

HA retains detailed history for ~10 days by default and long-term statistics (hourly/daily) indefinitely. The daily summary JSONs provide permanent 15-min resolution records beyond the 10-day HA history window.
