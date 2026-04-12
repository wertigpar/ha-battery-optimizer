# Battery Optimizer — Home Assistant Custom Integration

A Home Assistant custom integration that optimizes battery charge/discharge schedules based on electricity spot prices, solar PV forecasts, and battery state. It generates a 96-slot (15-minute resolution) daily schedule and pushes it to a battery system via a rolling 24-hour E2E override window. Integration in mainly built to work together with Emaldo Home Assistant custom component.

Integration is still prettu much in Proof-of-concept stage 

## How It Works

```
 ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
 │  Spot Price   │   │   Solcast     │   │  Battery SoC │
 │   Sensor      │   │  PV Forecast  │   │   Sensor     │
 └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
        │                  │                   │
        └──────────┬───────┴───────────────────┘
                   ▼
          ┌────────────────┐
          │   Greedy        │
          │   Optimizer     │   96 × 15-min slots
          │   (optimizer.py)│──────────────────────┐
          └────────────────┘                       │
                                                   ▼
                                           ┌──────────────┐
                                           │   Emaldo      │
                                           │   apply_bulk_ │
                                           │   schedule     │
                                           └──────────────┘
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

**Smart override logic:**

The optimizer compares its plan against the battery's internal AI schedule (read from the Emaldo integration). Only slots where the optimizer disagrees with the battery's internal plan are overridden. Matching slots are left to follow the internal schedule (value 128 = no override). The schedule is pushed as a rolling 96-slot E2E packet: positions at or after the current time-of-day slot carry today's plan, while positions before it carry tomorrow's plan (when available). After overrides are applied, the Emaldo schedule state is refreshed from the battery.

## Prerequisites

| Requirement | Details |
|---|---|
| **Home Assistant** | 2024.1+ |
| **Emaldo integration** | Must be installed and configured. The optimizer calls `emaldo.apply_bulk_schedule` to push the schedule. |
| **Spot price sensor** | A sensor with a `data` attribute containing 15-minute price entries (e.g. an Entso-E / Nordpool integration). See [Price Sensor Format](#price-sensor-format). |
| **Solcast PV integration** *(optional)* | [Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar) with `detailedForecast` attribute on today/tomorrow sensors. If not available, solar production is assumed zero. |
| **Battery SoC sensor** | A sensor reporting battery state of charge as a percentage (0–100). Typically `sensor.emaldo_battery_soc`. |

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
| **Spot price sensor** | Entity ID of your electricity price sensor | `sensor.electricity_prices` |
| **Solcast today sensor** | Entity ID of the Solcast today forecast sensor | `sensor.solcast_pv_forecast_forecast_today` |
| **Solcast tomorrow sensor** | Entity ID of the Solcast tomorrow forecast sensor | `sensor.solcast_pv_forecast_forecast_tomorrow` |
| **Battery SoC sensor** | Entity ID of battery state of charge sensor | `sensor.power_store_battery_soc` |
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
| **Base household load** | Estimated constant household load in kW | `1.0` |
| **Battery purchase price** | Purchase price of the battery system (€) for wear cost calculation | `9000` |
| **Battery lifetime cycles** | Expected number of full charge-discharge cycles | `10000` |
| **Idle power consumption** | Constant power draw of the battery unit itself (kW). Drains SoC even when idle. | `0.1` |
| **Idle slot strategy** | Controls what happens for slots where the optimizer has no action (see below). | `full_control` |
| **SoC guard interval** | How often (minutes) to actively update the discharge floor marker. See [SoC Guard](#soc-guard). | `0` (disabled) |

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

### Price Model

The optimizer applies fees to the raw spot price for each 15-minute slot:

```
buy_price  = spot_price × VAT_multiplier + transfer_fee_buy
sell_price = max(spot_price − sales_commission, 0)
```

Self-consumption discharge (existing stored energy) is scheduled when:

```
buy_price > wear_cost
```

Round-trip trades (buy cheap → discharge later) are scheduled when:

```
buy_saved > buy_charged / (η_charge × η_discharge) + wear_cost
```

where `wear_cost = battery_price / (lifetime_cycles × capacity_kwh)` is the full
round-trip degradation cost per kWh (e.g. 9000 / 10000 / 15 = 0.06 €/kWh).

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
- **Solar pre-computation**: estimated solar energy per slot is reduced by idle drain.
- **Discharge budget**: available usable energy is discounted by the cumulative idle drain of remaining slots.

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

The integration creates 5 sensor entities:

| Sensor | Description | Attributes |
|---|---|---|
| **Optimizer Status** | Current state: `idle`, `active`, or `scheduled` | `reason`, `charge_slots`, `discharge_slots`, `idle_slots`, `soc_guard_marker` |
| **Last Optimization** | Timestamp (device class: timestamp) of the last optimizer run | — |
| **Current Slot Action** | What the battery is doing right now: `charge`, `discharge`, `idle`, `none`, `unknown` | `slot_index`, `slot_value`, `buy_price`, `sell_price`, `solar_kw`, `soc_after` |
| **Estimated Daily Savings** | Estimated profit/savings for the current schedule (€) | — |
| **Schedule Chart** | Summary string (e.g. `5C 8D 83I`) with full schedule in attributes | `schedule` (list of 96–192 slots), `total_profit`, `activated_time`, `soc_guard_marker` |

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
    "profit": 0.0
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
    "profit": -0.0132
  },
  ...
]
```

When tomorrow's prices are available, the list extends to 192 entries. Each entry has `day: 0` (today) or `day: 1` (tomorrow). The `slot` field is 0–95 within each day.

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
  title: Price, SoC & Solar
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
    height: 250px
  legend:
    show: true
  yaxis:
    - id: soc
      min: 0
      max: 100
      decimalsInFloat: 0
      title:
        text: "SoC %"
    - id: price
      opposite: true
      decimalsInFloat: 1
      title:
        text: "c/kWh"
    - id: solar
      show: false
series:
  - entity: sensor.battery_optimizer_schedule_chart
    name: Battery SoC
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
      return schedule.map(s => [
        today.getTime() + s.day * 86400000 + s.slot * 15 * 60000,
        s.soc
      ]);
  - entity: sensor.battery_optimizer_schedule_chart
    name: Buy Price
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
    name: Sell Price
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
```

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
├── __init__.py          # HA entry setup, platform forwarding
├── config_flow.py       # UI config + options flow (14 parameters)
├── const.py             # All constants, defaults, slot encoding
├── coordinator.py       # Data gathering, trigger management, Emaldo push
├── manifest.json        # Integration metadata
├── optimizer.py         # Greedy solver — core optimization algorithm
├── sensor.py            # 5 sensor entities
├── services.py          # run_optimizer + clear_schedule services
├── services.yaml        # Service descriptions for UI
└── strings.json         # Translation strings
```
