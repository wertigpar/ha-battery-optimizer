# Emaldo Battery Optimizer — Home Assistant Custom Integration

![Example Home Assistant dashboard for Battery Optimizer](dashboard.png)

A Home Assistant custom integration that optimizes Emaldo battery charge/discharge schedules based on electricity spot prices, solar PV forecasts, and battery state. It generates a 96-slot (15-minute resolution) daily schedule and pushes it to a battery system via a rolling 24-hour E2E override window. Integration is mainly built to work together with Emaldo Home Assistant custom component.

## How It Works

```
 ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
 │  Spot Price  │   │   Solcast    │   │  Battery SoC │
 │   Sensor     │   │  PV Forecast │   │   Sensor     │
 └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
        │                  │                  │
        └──────────┬───────┴──────────────────┘
                   ▼
          ┌─────────────────────────────────────┐
          │          Greedy Optimizer           │
          │             (optimizer.py)          │
          │  • 96-slot battery schedule         │
          │  • thirdparty_pv_slots[96] (bool)   │
          └────────────┬──────────────┬─────────┘
                       │              │
           ┌───────────▼──┐   ┌───────▼────────────────┐
           │   Emaldo     │   │  switch.power_store_   │
           │ apply_bulk_  │   │  third_party_pv         │
           │  schedule    │   │  (PV sell strategy)     │
           └──────────────┘   └─────────────────────────┘
```

**Optimization strategy (greedy, self-consumption model):**

The Emaldo battery load-matches during discharge — it covers household load
only and does not export to grid. Discharge value therefore equals the grid
buy price avoided (self-consumption), not the sell/export price.

1. Identify solar surplus slots — battery idle mode absorbs excess PV for free.
2. Rank non-solar slots by buy price (most expensive first for discharge).
3. Discharge existing energy when `buy_price > wear_cost` (self-consumption saves money).
4. Round-trip trades when the price spread covers efficiency losses + wear.
5. Grid charge only the deficit that solar + existing SoC cannot cover.
6. **PV sell strategy** (optional): when enabled, computes a parallel `thirdparty_pv_slots[96]` plan. A single cutover time T (≤ noon by default) is chosen as the latest moment where solar energy remaining after T can still fill the battery to 100%. Solar before T is sold to the grid (PV switch OFF) for revenue; solar from T onward charges the battery uninterrupted.

**Smart override logic:**

The optimizer compares its plan against the battery's internal AI schedule (read from the Emaldo integration). Only slots where the optimizer disagrees with the battery's internal plan are overridden. Matching slots are left to follow the internal schedule (value 128 = no override). The schedule is pushed as a rolling 96-slot E2E packet: positions at or after the current time-of-day slot carry today's plan, while positions before it carry tomorrow's plan (when available). After overrides are applied, the Emaldo schedule state is refreshed from the battery.

## Prerequisites

| Requirement | Details |
|---|---|
| **Home Assistant** | 2024.1+ |
| **Emaldo integration** | Must be installed and configured. The optimizer calls `emaldo.apply_bulk_schedule` to push the schedule. Battery SoC and balancing state are auto-discovered from the selected Emaldo entry. |
| **Spot price sensor** | Required only when **Price data source** is set to `sensor`. A sensor with a `data` attribute containing 15-minute price entries (e.g. an Entso-E / Nordpool integration). See [Price Sensor Format](#price-sensor-format). When using `emaldo` (default), prices come from the Emaldo integration automatically. |
| **Solcast PV integration** *(optional)* | [Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar) with `detailedForecast` attribute on today/tomorrow sensors. If not available, solar production is assumed zero. |

> **Note — Emaldo internal solar forecast:** The Emaldo `schedule_chart` entity (`sensor.power_store_schedule_chart`) contains a `solar` field per slot, but this data is only populated when the Emaldo device is configured as a model that supports solar (e.g. **Store+Solar** or **3rd Party PV enabled**). When the device model is **Store** with **3rd Party PV = off**, the Emaldo backend does not provide solar forecast data and the `solar` field is always zero. This means Emaldo's internal solar forecast **cannot be used as a Solcast replacement** in that configuration. Any future feature that reads solar data from Emaldo must first check whether the device model/configuration actually supplies it, and fall back to zero (or Solcast) if not.

## Installation

1. Copy the `battery_optimizer` folder into your Home Assistant `custom_components/` directory:

   ```
   custom_components/
   ├── battery_optimizer/
   │   ├── __init__.py
   │   ├── config_flow.py
   │   ├── const.py
   │   ├── coordinator.py
   │   ├── manifest.json
   │   ├── optimizer.py
   │   ├── sensor.py
   │   ├── services.py
   │   ├── services.yaml
   │   └── strings.json
   └── emaldo/
       └── ...
   ```

2. Restart Home Assistant.

3. Go to **Settings → Devices & Services → Add Integration → Battery Optimizer**.

## Configuration

All parameters are set through the UI config flow. No YAML configuration needed.

### Config Flow Fields

| Field | Description | Default |
|---|---|---|
| **Price data source** | Where to read spot prices from. `emaldo` = use Emaldo's internal Nord Pool data (no extra sensor needed, 15-min resolution). `sensor` = use an external price sensor. | `emaldo` |
| **Spot price sensor** | Entity ID of your electricity price sensor. Only used when **Price data source** is `sensor`. | `sensor.electricity_prices` |
| **Solcast today sensor** | Entity ID of the Solcast today forecast sensor | `sensor.solcast_pv_forecast_forecast_today` |
| **Solcast tomorrow sensor** | Entity ID of the Solcast tomorrow forecast sensor | `sensor.solcast_pv_forecast_forecast_tomorrow` |
| **VAT multiplier** | VAT multiplier applied to spot price when buying (1.255 = 25.5% Finnish VAT) | `1.255` |
| **Grid transfer fee** | Transfer fee added to buy price (€/kWh) | `0.0776` |
| **Sales commission** | Commission deducted from sell price (€/kWh) | `0.003` |
| **Battery capacity** | Total battery capacity in kWh | `15.0` |
| **Max charge power** | Maximum charge rate in kW | `10.0` |
| **Max discharge power** | Maximum discharge rate in kW | `10.0` |
| **Charge efficiency** | Charge efficiency (0.5–1.0) | `0.9` |
| **Discharge efficiency** | Discharge efficiency (0.5–1.0) | `0.9` |
| **Min SoC** | Minimum allowed state of charge (%) | `20` |
| **Max SoC** | Maximum allowed state of charge (%) | `100` |
| **Base household load** | Estimated constant household load in kW. Used when auto-tune is disabled. | `1.0` |
| **Battery wear cost** | Cost per kWh cycled (€/kWh) — accounts for battery degradation. Typical LFP: 1–5 snt/kWh. | `0.03` |
| **Idle power consumption** | Constant power draw of the battery unit itself (kW). Drains SoC even when idle. | `0.1` |
| **Auto-tune base load** | When enabled, the optimizer computes base load from recorder history instead of using the static value above. | `false` |
| **Household load power sensor** | Entity ID of a combined household load power sensor in Watts (e.g. `sensor.combined_power`). Required when auto-tune is enabled. | *(empty)* |
| **Idle slot strategy** | Controls what happens for slots where the optimizer has no action (see below). | `full_control` |
| **SoC guard interval** | How often (minutes) to actively update the discharge floor marker. See [SoC Guard](#soc-guard). | `0` (disabled) |
| **Emaldo battery device** | Emaldo config entry to use. Shown as a dropdown — select the correct system when multiple Emaldo devices are installed. | first found |
| **Enable PV sell strategy** | Initial default for the live `switch.battery_optimizer_pv_strategy` entity. When `true`, the optimizer will plan PV-to-grid slots on sunny days instead of always charging the battery. See [PV Sell Strategy](#pv-sell-strategy). | `false` |
| **Min solar forecast for PV sell** | Minimum Solcast forecast (kWh) required to activate PV sell strategy. Below this threshold the strategy is skipped (cloudy day guard). | `10.0` |
| **Solar forecast mode** | Which Solcast percentile to use for charge planning. `p10` (default) uses the pessimistic 10th-percentile forecast — weather-aware, causes the optimizer to add more grid charge slots on cloudy/uncertain days. `p50` uses the median, which can leave the battery undercharged when actual solar is lower than expected. | `p10` |
| **Min solar fraction for PV sell** | Minimum fraction of needed solar energy (from current slot onward) required to allow selling. If available solar is below `needed_kwh × this_value`, selling is skipped entirely. Configurable as `pv_sell_solar_margin`. | `0.95` |
| **Min sell price for PV sell** | Minimum sell price (€/kWh) required to activate selling for a slot. Set to `0.0` to sell at any positive price; raise it to only sell when prices are attractive. Configurable as `pv_sell_min_price_spread`. | `0.0` |

All parameters can be changed later via **Settings → Devices & Services → Battery Optimizer → Configure**.

### Idle Slot Strategy

When the optimizer decides a slot should be "idle" (no charge/discharge), the strategy setting controls how that idle instruction is sent to the Emaldo battery:

| Strategy | Value | Behaviour |
|---|---|---|
| **Full control** | `full_control` | Force idle (SLOT_IDLE = 0x00) for **all** idle slots. The optimizer fully controls the battery 24/7 — the internal AI never acts on its own. **Default and recommended.** |
| **Solar guard** | `solar_guard` | Force idle only for slots **before** the first solar production of the day. After solar starts, idle slots are left as "no override" (0x80) letting the internal AI decide. Prevents overnight grid charging while giving the AI freedom during/after solar hours. |
| **Smart override** | `smart_override` | Force idle only when **both** conditions are met: (1) the internal AI plans to **charge** at that slot, and (2) solar production is expected later in the day. Most targeted — only blocks the specific problematic case of pre-solar grid charging that the AI initiates. |

> **Background:** The Emaldo battery has an internal AI that makes its own charge/discharge decisions. When the optimizer sends `SLOT_NO_OVERRIDE` (0x80), the internal AI is free to act — which can lead to unwanted overnight grid charging that fills the battery before solar production arrives. The `full_control` strategy prevents this by explicitly forcing the battery idle for slots the optimizer doesn't need.

### SoC Guard

The Emaldo battery uses a single global "Battery Range" setting (high/low markers) that applies to **all** discharge slots simultaneously. This means per-slot discharge thresholds (e.g. "discharge to 75% at 17:00, then to 60% at 19:00") cannot be achieved through the slot values alone — the firmware treats `high_marker` as a global discharge floor.

The SoC Guard feature works around this limitation by **actively rotating the discharge floor** at a configurable interval:

| Interval | Value | Behaviour |
|---|---|---|
| **Disabled** | `0` | No SoC guard — discharge uses the default markers. **Default.** |
| **15 min** | `15` | Update the discharge floor every 15 minutes |
| **30 min** | `30` | Update every 30 minutes |
| **60 min** | `60` | Update every hour |
| **120 min** | `120` | Update every 2 hours |

**How it works:**

At each interval tick, the optimizer looks forward in the current schedule by the interval duration and finds the lowest planned discharge SoC within that window. It then sets `high_marker` to that value, preventing the battery from discharging below the planned floor — even if unexpected loads appear.

**Example** (30-minute interval):
- **16:30** — Plan says discharge to 75% by 17:00 → sets `high_marker = 75`
- **17:00** — Plan says discharge to 60% by 17:30 → sets `high_marker = 60`
- **17:30** — No discharge planned → sets `high_marker = soc_min` (most permissive)

**Use case:** The sauna kicks on at 16:45 during what should be a moderate discharge window. Without SoC Guard, the battery could empty to `soc_min` due to the sudden load spike. With guard enabled at 30-min interval, the discharge stops at 75% — preserving energy for the planned evening peak discharge.

The current SoC guard marker is exposed in the **Optimizer Status** and **Schedule Chart** sensor attributes as `soc_guard_marker`.

### Auto Base Load

When **Auto-tune base load** is enabled, the optimizer queries the HA recorder for 14 days of daily statistics from the configured **Household load power sensor** (W), computes a 7-day rolling average, and uses that as the base load for every optimization run.

**How it works:**

1. At each optimizer run, `statistics_during_period` is called for the load sensor with `"day"` bucket and `"mean"` statistic.
2. Daily mean values (W) are converted to kW. Days with no positive data are skipped.
3. The most recent 7 days of daily average kW are averaged.
4. The result is clamped to ±50% of the configured static base load (to limit the effect of anomalous days).
5. The final value is applied for this run and exposed on the **Auto Base Load** sensor.

**Falls back to the static `base_load_kw` if:**
- Auto-tune is disabled
- No sensor is configured
- The recorder component is unavailable
- Fewer than 3 days of data exist

**Recommended sensor:** `sensor.combined_power` — a template sensor that calculates actual household consumption from all sources:
```
grid_power - battery_power + solar_power  (in Watts)
```

### Price Model

The optimizer applies fees to the raw spot price for each 15-minute slot:

```
buy_price  = spot_price × VAT_multiplier + transfer_fee_buy
sell_price = spot_price − sales_commission
```

> **Negative spot prices:** When `spot < 0`, the VAT multiplier is clamped to 1.0 — the subsidy passes through at face value without amplification by 1.255. A negative effective buy price is treated as a strong incentive to charge from the grid.

Self-consumption discharge (existing stored energy) is scheduled when:

```
buy_price > wear_cost
```

Round-trip trades (buy cheap → discharge later) are scheduled when:

```
buy_saved > buy_charged / (η_charge × η_discharge) + wear_cost
```

where `wear_cost` is configured directly as **Battery wear cost** (default 0.03 €/kWh = 3 snt/kWh).

### Idle Power Drain

The battery unit draws constant power (default 0.1 kW = 100 W) regardless of mode.
This drains the SoC during every 15-minute slot:

```
idle_drain_per_slot = idle_power_kw × 0.25 h = 0.025 kWh  (at 0.1 kW)
daily_drain          = 0.025 × 96 = 2.4 kWh                (~16 % of 15 kWh)
```

The optimizer accounts for this in all calculations:

- **SoC simulation**: idle drain is subtracted from every planned slot (charge, discharge, and idle).
  During idle slots with solar surplus the solar energy offsets the drain, so a full battery stays at 100 % when surplus exceeds idle draw.
- **Discharge budget**: a forward idle-only SoC simulation runs before planning to find the peak SoC the battery will reach from solar charging. The discharge budget equals `peak_soc − soc_min`, ensuring evening discharge is planned even when the current SoC is below `soc_min` at optimization time (e.g. SoC 12% at 8:30 AM with full solar fill expected by 13:00).

## Price Sensor Format

The integration reads the `data` attribute from your spot price sensor. It expects a list of objects with `start`, `end`, and `price` keys at 15-minute resolution:

```yaml
data:
  - start: "2026-03-19 00:00:00"
    end: "2026-03-19 00:15:00"
    price: 1.23
  - start: "2026-03-19 00:15:00"
    end: "2026-03-19 00:30:00"
    price: 1.25
  # ... 96 entries per day
```

**Unit detection:** If the sensor's `unit_of_measurement` contains `snt`, `cent`, or `c/kWh`, prices are automatically divided by 100 to convert to €/kWh.

The sensor may include both today's and tomorrow's data in the same list — entries are split by date automatically.

**Supported integrations:**
- Finnish electricity price integrations producing 15-min `data` attribute (tested)
- Any custom sensor following the above format

> **Note:** The classic Nordpool integration format with `today`/`tomorrow` hourly attributes is also supported — if 24 hourly values are given, each is expanded to 4 × 15-minute slots.

## Sensors

The integration creates 7 sensor entities:

| Entity | Type | Description | Attributes |
|---|---|---|---|
| **Optimizer Status** | sensor | Current state: `idle`, `active`, or `scheduled` | `reason`, `charge_slots`, `discharge_slots`, `idle_slots`, `soc_guard_marker`, `balancing_active` |
| **Last Optimization** | sensor | Timestamp of the last optimizer run | — |
| **Current Slot Action** | sensor | What the battery is doing right now: `charge`, `discharge`, `idle`, `none`, `unknown` | `slot_index`, `slot_value`, `buy_price`, `sell_price`, `solar_kw`, `soc_after` |
| **Estimated Daily Savings** | sensor | Estimated profit/savings for the current schedule (€) | — |
| **Schedule Chart** | sensor | Summary string (e.g. `5C 8D 83I`) with full schedule in attributes | `schedule` (list of 96–192 slots), `total_profit`, `activated_time`, `soc_guard_marker`, `soc_history` |
| **Auto Base Load** | sensor | The base load value (kW) currently used by the optimizer. | — |
| **Plan Accuracy** | sensor | Signed discharge error in kWh since last optimizer run (positive = more discharge than planned, negative = less). | — |

## Switches

The integration creates 2 switch entities:

| Entity | Description |
|---|---|
| **PV Sell Strategy** | Enable/disable the PV sell strategy at runtime. When ON, the optimizer plans solar slots where third-party PV is disabled so solar is exported to grid at spot price. When OFF, solar is always used to charge the battery. Toggling triggers an immediate optimizer re-run and re-schedules PV switch transitions for the rest of the day. State persists across HA restarts. |
| **Enable Emaldo control** | Enable/disable battery control via the Emaldo integration. When ON (default), the optimizer can push schedules to the battery via `apply_bulk_schedule`. When OFF, the optimizer runs and computes the schedule but does not send any commands to Emaldo — useful for testing, debugging, or running in "dry-run" mode. State persists across HA restarts. |

### Schedule Chart Attribute Format

The `schedule` attribute on the Schedule Chart sensor contains the plan for today (96 slots) plus tomorrow when prices are available (up to 192 slots):

```json
[
  {
    "slot": 0,
    "time": "00:00",
    "day": 0,
    "action": "idle",
    "state": "Idle",
    "target_soc": null,
    "value": 0,
    "buy": 0.3078,
    "sell": 0.0003,
    "solar": 0.0,
    "soc": 20.0,
    "profit": 0.0,
    "pv_sell": false
  },
  {
    "slot": 22,
    "time": "05:30",
    "day": 1,
    "action": "idle",
    "state": "Idle",
    "target_soc": null,
    "value": 0,
    "buy": 0.1415,
    "sell": 0.0479,
    "solar": 0.844,
    "soc": 70.3,
    "profit": 0.0,
    "pv_sell": true
  },
  {
    "slot": 32,
    "time": "08:00",
    "day": 0,
    "action": "charge",
    "state": "Charge",
    "target_soc": 100,
    "value": 100,
    "buy": 0.0528,
    "sell": 0.0003,
    "solar": 2.5,
    "soc": 45.0,
    "profit": -0.0132,
    "pv_sell": false
  },
  ...
]
```

When tomorrow's prices are available, the list extends to 192 entries. Each entry has `day: 0` (today) or `day: 1` (tomorrow). The `slot` field is 0–95 within each day.

**`pv_sell`** — `true` means the third-party PV switch is planned to be OFF for this slot — solar energy is exported to the grid at spot price rather than charging the battery. Always `false` when the PV sell strategy switch is disabled or solar is below 0.1 kW.

The `activated_time` attribute shows the time window that was sent to the battery as override commands, e.g. `"Today 14:15–23:45 + Tomorrow 00:00–06:30"`. This indicates how far forward the schedule has been activated on the battery hardware. The Emaldo E2E override uses a rolling 24-hour window: a single 96-slot push covers today's remaining slots plus (when tomorrow's prices are available) tomorrow's early slots.

This can be used with HA dashboard cards (e.g. ApexCharts) to visualize the schedule.

## Services

### `battery_optimizer.run_optimizer`

Manually trigger an optimization run.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `reason` | string | `manual` | Tag for logging why this run was triggered |
| `force` | boolean | `true` | If `false`, skip optimization when SoC deviation is small |

**Example automation:**
```yaml
service: battery_optimizer.run_optimizer
data:
  reason: "evening_recheck"
  force: true
```

### `battery_optimizer.clear_schedule`

Remove all battery override slots, reverting the battery to its internal (built-in) schedule. Calls `emaldo.reset_to_internal` under the hood.

```yaml
service: battery_optimizer.clear_schedule
```

## Automatic Triggers

The optimizer runs automatically based on:

### Optimizer Re-run Interval

The optimizer re-runs periodically based on the **Optimizer re-run interval** setting (configurable: 15 / 30 / 60 / 120 minutes, default 120).

In addition, a **fixed midnight checkpoint** always runs at **00:01** to re-optimize for the new day.

All periodic runs are **conditional** (`force=False`) — they will skip if the actual battery SoC is within 10% of the planned SoC.

### Price Sensor State Change

When the spot price sensor updates (e.g. tomorrow's prices become available), the optimizer checks `tomorrow_valid` or parses the new data. If tomorrow's prices are detected, a **forced** re-optimization runs immediately.

## Emaldo Slot Encoding

The optimizer maps actions to Emaldo override byte values:

| Value | Meaning |
|---|---|
| `0` | Idle — battery does nothing |
| `1–100` | Charge to N% SoC |
| `128` | No override — follow internal schedule |
| `129–255` | Discharge down to (256 − value)% SoC — load-matched, covers household load only |

## Example Automations

### Re-optimize when Solcast updates

```yaml
automation:
  - alias: "Re-optimize on solar forecast update"
    trigger:
      - platform: state
        entity_id: sensor.solcast_pv_forecast_forecast_today
    action:
      - service: battery_optimizer.run_optimizer
        data:
          reason: "solcast_update"
          force: true
```

### Clear schedule before maintenance

```yaml
automation:
  - alias: "Clear battery schedule"
    trigger:
      - platform: state
        entity_id: input_boolean.battery_maintenance
        to: "on"
    action:
      - service: battery_optimizer.clear_schedule
```

### Dashboard card (ApexCharts)

Requires [apexcharts-card](https://github.com/RomRider/apexcharts-card) from HACS.

#### Action Plan

Shows the optimizer's planned battery schedule for every 15-minute slot as
uniform-height colored bars. Three states: **Charge** (from grid), **Discharge**,
and **Idle** (holds battery; excess solar charges naturally during idle).

```yaml
type: custom:apexcharts-card
header:
  title: Battery Action Plan
  show: true
  show_states: false
graph_span: 48h
span:
  start: day
now:
  show: true
  label: Now
  color: red
apex_config:
  chart:
    height: 150px
    stacked: true
  plotOptions:
    bar:
      columnWidth: "100%"
  legend:
    show: true
  yaxis:
    - show: false
      min: 0
      max: 1.1
series:
  - entity: sensor.battery_optimizer_schedule_chart
    name: Charge
    type: column
    color: "#2ecc71"
    opacity: 0.9
    show:
      in_header: false
      legend_value: false
    data_generator: |
      const schedule = entity.attributes.schedule || [];
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      return schedule.map(s => [
        today.getTime() + s.day * 86400000 + s.slot * 15 * 60000,
        s.state === 'Charge' ? 1 : null
      ]);
  - entity: sensor.battery_optimizer_schedule_chart
    name: Discharge
    type: column
    color: "#e74c3c"
    opacity: 0.9
    show:
      in_header: false
      legend_value: false
    data_generator: |
      const schedule = entity.attributes.schedule || [];
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      return schedule.map(s => [
        today.getTime() + s.day * 86400000 + s.slot * 15 * 60000,
        s.state === 'Discharge' ? 1 : null
      ]);
  - entity: sensor.battery_optimizer_schedule_chart
    name: Idle
    type: column
    color: "#bdc3c7"
    opacity: 0.5
    show:
      in_header: false
      legend_value: false
    data_generator: |
      const schedule = entity.attributes.schedule || [];
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      return schedule.map(s => [
        today.getTime() + s.day * 86400000 + s.slot * 15 * 60000,
        s.state === 'Idle' ? 1 : null
      ]);
```

- **Green** = charge from grid at cheap rates
- **Red** = discharge to self-consume (avoid grid purchase)
- **Gray** = idle — hold battery, excess solar charges naturally

#### Price, SoC & Solar

Shows electricity prices, the planned SoC trajectory, and solar forecast to explain *why*
the optimizer chose each action.

```yaml
type: custom:apexcharts-card
header:
  title: Total Price, SoC estimate & Solar forecast
  show: true
  show_states: false
graph_span: 48h
span:
  start: day
now:
  show: true
  label: Now
  color: red
yaxis:
  - id: soc
    min: 0
    max: 100
    apex_config:
      decimalsInFloat: 0
      title:
        text: "SoC %"
  - id: price
    opposite: true
    min: ~0
    apex_config:
      decimalsInFloat: 1
      title:
        text: "c/kWh"
  - id: solar
    show: false
    min: 0
    max: 7
    apex_config:
      title:
        text: "kW"
apex_config:
  chart:
    height: 250px
  legend:
    show: true
series:
  - entity: sensor.battery_optimizer_schedule_chart
    name: Battery SoC Forecast
    type: area
    yaxis_id: soc
    stroke_width: 2
    opacity: 0.15
    color: "#9b59b6"
    show:
      in_header: false
      legend_value: false
    data_generator: |
      const schedule = entity.attributes.schedule || [];
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      const now = Date.now();
      return schedule
        .filter(s => today.getTime() + s.day * 86400000 + s.slot * 15 * 60000 >= now)
        .map(s => [today.getTime() + s.day * 86400000 + s.slot * 15 * 60000, s.soc]);
  - entity: sensor.battery_optimizer_schedule_chart
    name: Buy Total Cost
    type: line
    yaxis_id: price
    stroke_width: 2
    color: "#3498db"
    show:
      in_header: false
      legend_value: false
    data_generator: |
      const schedule = entity.attributes.schedule || [];
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      return schedule.map(s => [
        today.getTime() + s.day * 86400000 + s.slot * 15 * 60000,
        Math.round(s.buy * 10000) / 100
      ]);
  - entity: sensor.battery_optimizer_schedule_chart
    name: Sell Total Profit
    type: line
    yaxis_id: price
    stroke_width: 2
    color: "#e67e22"
    show:
      in_header: false
      legend_value: false
    data_generator: |
      const schedule = entity.attributes.schedule || [];
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      return schedule.map(s => [
        today.getTime() + s.day * 86400000 + s.slot * 15 * 60000,
        Math.round(s.sell * 10000) / 100
      ]);
  - entity: sensor.battery_optimizer_schedule_chart
    name: Solar Forecast
    type: area
    yaxis_id: solar
    stroke_width: 1
    opacity: 0.2
    color: "#f1c40f"
    show:
      in_header: false
      legend_value: false
    data_generator: |
      const schedule = entity.attributes.schedule || [];
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      return schedule.map(s => [
        today.getTime() + s.day * 86400000 + s.slot * 15 * 60000,
        s.solar
      ]);
  - entity: sensor.power_store_battery_soc
    name: Actual SoC History
    type: line
    yaxis_id: soc
    stroke_width: 2
    color: "#e74c3c"
    extend_to: false
    show:
      in_header: false
      legend_value: false
```

## PV Sell Strategy

The PV sell strategy controls `switch.power_store_third_party_pv` (the Emaldo "third-party PV" switch) slot by slot to sell morning solar energy directly to the grid at a better price instead of always charging the battery first.

### Background

Without this feature the battery always absorbs available PV before selling. On a clear summer day the battery fills by mid-morning (~09:00), and all subsequent solar is sold at whatever the current (often low) midday spot price is.

The optimizer can plan a more profitable sequence:

1. Morning solar (05:00–08:00): PV switch **OFF** → solar sells to grid at the higher morning spot price.
2. Mid-morning (08:00–10:00): PV switch **ON** → solar charges battery at zero cost.
3. Afternoon / evening: battery discharges during expensive peak hours as usual.

### How the planner works (`_plan_pv_sell_slots`)

The function runs after the main greedy optimizer has computed the battery action schedule.

**Step 1 — Compute remaining solar (backward scan):**
A backward pass builds `remaining_solar[s]` = total net solar energy (after base load, capped at `max_charge_kw`, times `charge_efficiency`) available from slot `s` to end-of-day. Grid-charge slots (`action == "charge"`) are excluded — they already fill the battery and are never overridden.

**Step 2 — Guard: insufficient total solar:**
If `remaining_solar[start_slot] < needed_kwh × pv_sell_solar_margin` (default 0.95, configurable), selling would risk not filling the battery — strategy is skipped and all slots default to PV-on.

**Step 3 — Find cutover T:**
T is the **latest** slot ≤ noon (slot 48, 12:00) where `remaining_solar[T] ≥ needed_kwh`. If post-noon solar alone is enough, T = noon and the full morning window is available for selling. If post-noon solar is insufficient (e.g. partial cloud), T is moved earlier until the remaining solar constraint is satisfied.

**Step 4 — Mark sell slots:**
All solar slots in `[start_slot, T)` with `sell_price > pv_sell_min_price_spread` (default 0.0 — sell at any positive price, configurable) are marked as sell (PV switch OFF). Solar slots from T onward are always kept as charge (PV switch ON).

**Step 5 — SoC trajectory correction (`_correct_soc_for_pv_sells`):**
After sell slots are finalised, `SlotPlan.soc_after` values (computed during the main greedy pass assuming all solar charged the battery) are corrected. A forward pass from the first sell slot recomputes `soc_after` in-place so the dashboard SoC forecast correctly shows a flat/draining morning and a rising ramp from the cutover time onward. Both today and tomorrow plans are corrected.

### Guard conditions

| Condition | Behaviour |
|---|---|
| PV strategy switch OFF | All slots default to PV enabled (no grid interaction) |
| Solcast forecast < `solar_sell_min_forecast_kwh` (default 10 kWh) | Strategy skipped for the day — cloudy-day guard |
| Solcast data unavailable | Strategy skipped |
| Grid-charge slot | Never overridden regardless of sell price |

### Live control — `switch.battery_optimizer_pv_strategy`

The strategy is toggled via the **PV Sell Strategy** switch entity. It uses `RestoreEntity` so the state survives HA restarts. Toggling it immediately triggers an optimizer re-run, which recomputes `thirdparty_pv_slots` and re-schedules all PV switch transitions for the rest of the day.

The coordinator cancels and rebuilds the `async_call_later` transition callbacks on every optimizer run, so the physical switch always follows the current plan.

---

#### Dashboard chart — Third-Party PV Plan

Requires [apexcharts-card](https://github.com/RomRider/apexcharts-card) from HACS.

Shows three states per 15-minute slot over 48 hours:
- **Green** (`PV → Battery`): solar production present, PV switch ON — charging battery
- **Orange** (`PV → Grid`): solar production present, PV switch OFF — selling to grid
- **Grey** (`No Solar`): solar ≤ 0.05 kW — night or overcast

```yaml
type: custom:apexcharts-card
header:
  title: Third-Party PV Plan
  show: true
  show_states: false
graph_span: 48h
span:
  start: day
now:
  show: true
  label: Now
  color: red
apex_config:
  chart:
    height: 150px
    stacked: true
  plotOptions:
    bar:
      columnWidth: "100%"
  legend:
    show: true
  yaxis:
    - show: false
      min: 0
      max: 1.1
series:
  - entity: sensor.battery_optimizer_schedule_chart
    name: PV → Battery
    type: column
    color: "#2ecc71"
    opacity: 0.9
    show:
      in_header: false
      legend_value: false
    data_generator: |
      const schedule = entity.attributes.schedule || [];
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      return schedule.map(s => [
        today.getTime() + s.day * 86400000 + s.slot * 15 * 60000,
        s.solar > 0.05 && !s.pv_sell ? 1 : null
      ]);
  - entity: sensor.battery_optimizer_schedule_chart
    name: PV → Grid
    type: column
    color: "#f39c12"
    opacity: 0.9
    show:
      in_header: false
      legend_value: false
    data_generator: |
      const schedule = entity.attributes.schedule || [];
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      return schedule.map(s => [
        today.getTime() + s.day * 86400000 + s.slot * 15 * 60000,
        s.solar > 0.05 && s.pv_sell ? 1 : null
      ]);
  - entity: sensor.battery_optimizer_schedule_chart
    name: No Solar
    type: column
    color: "#bdc3c7"
    opacity: 0.3
    show:
      in_header: false
      legend_value: false
    data_generator: |
      const schedule = entity.attributes.schedule || [];
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      return schedule.map(s => [
        today.getTime() + s.day * 86400000 + s.slot * 15 * 60000,
        s.solar <= 0.05 ? 1 : null
      ]);
```

---

## Troubleshooting

### "Cannot optimize: no Nordpool prices available"

The price sensor's `data` attribute is empty, missing, or has fewer than 10 price entries for today's date. Verify:

1. The sensor entity ID is correct in the config.
2. The sensor has a `data` attribute (check Developer Tools → States).
3. The `data` list contains entries with `start`, `end`, and `price` keys.
4. Entries cover today's date.

### "Emaldo service 'apply_bulk_schedule' not available"

The Emaldo integration is not loaded or its services haven't registered yet. The optimizer will compute the schedule but cannot apply it. Ensure the Emaldo integration is installed and configured.

### Schedule not updating

- Check logs for `battery_optimizer` entries.
- Verify checkpoint times are in the future (optimizer only plans from the current slot onward).
- Try a manual run: **Developer Tools → Services → `battery_optimizer.run_optimizer`**.

## Architecture

```
battery_optimizer/
├── __init__.py          # HA entry setup, platform forwarding (sensor + button + switch)
├── config_flow.py       # UI config + options flow
├── const.py             # All constants, defaults, slot encoding
├── coordinator.py       # Data gathering, trigger management, Emaldo push, PV strategy
├── manifest.json        # Integration metadata
├── optimizer.py         # Greedy solver — core optimization algorithm + PV sell planner
├── sensor.py            # 7 sensor entities including plan_accuracy, schedule_chart
├── services.py          # run_optimizer + clear_schedule services
├── services.yaml        # Service descriptions for UI
├── switch.py            # PvStrategySwitch — PV sell strategy toggle entity
└── strings.json         # Translation strings
```
