# Cost Breakdown Attributes Design

## Problem

Baseline cost calculation uses `net_loads` (base_load − solar), which makes the
baseline negative on high-solar days (export revenue > import cost). The user
expects baseline = "cost of buying all base load from grid", which should always
be positive. Additionally, cost sensors lack subcost attributes — users can see
the total but not where the money goes (energy, transfer fee, tax, commission).

## Goals

1. Fix baseline to represent grid-only cost (always positive)
2. Fix emaldo idle case to use same corrected logic
3. Add fee decomposition attributes to all three cost sensors
4. Handle Finnish household rules: no export tax, import tax = 0 when spot < 0

## Changes

### 1. Baseline calculation fix (optimizer.py ~1608-1616)

**Current:**
```python
baseline_import = max(net_loads[s], 0) * SLOT_DURATION_HOURS
baseline_export = max(-net_loads[s], 0) * SLOT_DURATION_HOURS
baseline_slot = bp * baseline_import - sp * baseline_export
```

**Fixed:**
```python
baseline_import = cfg.base_load_kw * SLOT_DURATION_HOURS
baseline_export = max(solar_15min[s] - cfg.base_load_kw, 0) * SLOT_DURATION_HOURS
baseline_slot = buy_prices[s] * baseline_import - sell_prices[s] * baseline_export
```

Rationale: Without battery, all base load is imported from grid. Solar surplus
above base load is exported. Baseline = import cost − export revenue.

### 2. Emaldo idle case fix (optimizer.py ~1677-1680)

**Current:**
```python
e_grid_kwh = max(net_loads[s], 0) * SLOT_DURATION_HOURS
```

**Fixed:**
```python
e_grid_kwh = cfg.base_load_kw * SLOT_DURATION_HOURS
e_export_kwh = max(solar_15min[s] - cfg.base_load_kw, 0) * SLOT_DURATION_HOURS
```

Same logic as baseline: idle = no battery, grid imports full base load.

### 3. Fee decomposition helpers (new functions in optimizer.py)

Finnish household electricity pricing:
- Buy = spot × VAT + transfer_fee + commission
- Sell = spot − commission (no VAT, no transfer fee)
- Import tax = max(0, spot) × (VAT − 1) — zero when spot is negative
- No export tax for household customers

```python
def _decompose_buy(price: float, cfg: BatteryConfig) -> tuple[float, float, float, float]:
    """Decompose buy_price into (energy, transfer, tax, commission) per kWh."""
    spot = (price - cfg.transfer_fee_buy - cfg.sales_commission) / cfg.vat_multiplier
    transfer = cfg.transfer_fee_buy
    tax = max(0.0, spot) * (cfg.vat_multiplier - 1)
    energy = price - transfer - tax - cfg.sales_commission
    return (energy, transfer, tax, cfg.sales_commission)


def _decompose_sell(price: float, cfg: BatteryConfig) -> tuple[float, float, float]:
    """Decompose sell_price into (energy, tax, commission) per kWh.
    
    No export tax for Finnish household customers.
    """
    spot = price + cfg.sales_commission
    return (spot, 0.0, cfg.sales_commission)
```

### 4. Cost accumulation loop — track grid import + export per slot

Replace single `actual_cost += bp*import - sp*export` with separate accumulators:

**Baseline accumulators:**
- `baseline_import_kwh`, `baseline_export_kwh`
- Decomposed: `bl_import_energy`, `bl_import_transfer`, `bl_import_tax`, `bl_import_commission`
- Decomposed: `bl_export_energy`, `bl_export_commission`

**Optimizer plan accumulators:**
- `total_grid_import_kwh`, `total_solar_export_kwh`
- Decomposed: `op_import_energy`, `op_import_transfer`, `op_import_tax`, `op_import_commission`
- Decomposed: `op_export_energy`, `op_export_commission`

**Emaldo plan accumulators:**
- `e_grid_import_kwh`, `e_export_kwh`
- Decomposed: `e_import_energy`, `e_import_transfer`, `e_import_tax`, `e_import_commission`
- Decomposed: `e_export_energy`, `e_export_commission`

### 5. Sensor attributes

**baseline_cost** (state = total baseline in EUR):
```
import_cost = import_energy + import_transfer + import_tax + import_commission
export_revenue = export_energy - export_commission
import_energy, import_transfer, import_tax, import_commission
export_energy, export_commission
remaining_slots = 71
```

**optimizer_plan_cost** (state = grid_cost + wear_cost):
```
grid_cost = baseline_cost - gross_savings
wear_cost = wear_total
cycled_kwh
grid_import_kwh, grid_export_kwh
grid_energy, grid_transfer, grid_tax, grid_commission
grid_export_energy, grid_export_commission
```

**emaldo_plan_cost** (state = emaldo_grid_cost - emaldo_wear_cost):
```
emaldo_grid_cost = pre-wear grid cost
emaldo_wear_cost = wear from emaldo cycles
emaldo_cycled_kwh
emaldo_import_kwh, emaldo_export_kwh
emaldo_energy, emaldo_transfer, emaldo_tax, emaldo_commission
emaldo_export_energy, emaldo_export_commission
```

**estimated_savings** (unchanged — baseline fix makes this correct automatically):
```
gross_savings = baseline - optimizer_grid_cost
wear_cost
cycled_kwh
savings_after_wear = gross_savings - wear_cost
```

### 6. Update breakdown functions

`optimizer_plan_cost_breakdown()` and `emaldo_plan_cost_breakdown()` return
the full decomposed dict instead of just grid_cost/wear_cost/cycled_kwh.

## Expected values after fix (today's data, slot 25, start_slot=25)

| Sensor | Before | After |
|--------|--------|-------|
| baseline_cost | -2.14 | ~5.31 |
| optimizer_plan_cost | 0.31 | ~0.31 |
| emaldo_plan_cost | 2.45 | ~3.62 |
| estimated_savings | -2.46 | ~5.00 |

## Files changed

- `custom_components/battery_optimizer/optimizer.py`
  - Add `_decompose_buy()`, `_decompose_sell()` helpers
  - Fix baseline calculation (lines ~1608-1616)
  - Fix emaldo idle case (lines ~1677-1680)
  - Add accumulator variables in cost loop
  - Update `optimise()` to pass decomposed values to `OptimizationResult`
  - Add fields to `OptimizationResult` dataclass for decomposed values
  - Update `optimizer_plan_cost_breakdown()` and `emaldo_plan_cost_breakdown()`

- `custom_components/battery_optimizer/sensor.py`
  - Update `optimizer_plan_cost` and `emaldo_plan_cost` sensor extra_state_attributes
  - Update `baseline_cost` sensor extra_state_attributes
  - Update `estimated_savings` sensor extra_state_attributes

## Verification

1. Run optimizer with live data, verify baseline is positive
2. Check all attribute sums match state values
3. Verify negative spot price handling (tax = 0)
4. Compare emaldo_plan_cost before/after (should increase due to fixed idle import)
