# Changes

## v0.2.0

### Added

- **HACS compatibility** — `hacs.json`, HACS badges + "Open HACS repository"
  button in README, GitHub links (`documentation`, `issue_tracker`,
  `codeowners`) in manifest.json, MIT `LICENSE`, `brand/icon.png`, repo topics,
  and `after_dependencies: recorder` (auto base-load history query). HACS
  action (topics/license/brands) and hassfest validation both green.

- **Tomorrow cost sensors** — New sensors (`Tomorrow Estimated Savings`,
  `Tomorrow Baseline Cost`, `Tomorrow Emaldo Cost`, `Tomorrow Optimizer Cost`)
  showing tomorrow's estimates after Nordpool publishes (~14:00). Read
  `coordinator.last_result_tomorrow`; show `unknown` before data is available.

### Fixed

- **PV sell strategy starvation** — the cutover scan used the plan-start SoC
  (`needed_kwh = soc_max − current_soc`), which understates the battery gap
  accumulated during the sell window (missed surplus absorption + base-load
  coverage). On sunny days the battery peaked ~78 % instead of 100 %, losing
  ~€0.40/day in evening-peak discharge. The cutover T is now derived by
  iteration: `_forward_soc_sim()` simulates the SoC entering the cutover under
  the sell window, the true need is re-derived, and T is re-scanned until
  stable (max 6 passes). A final starvation guard re-validates T against the
  re-simulated need and skips selling for the day if solar after T cannot
  cover it. Floor recovery now precedes selling: below the SoC floor, solar
  recharges to `soc_min + buffer` before any export; if unrecoverable, selling
  is skipped.

- **Cost sensor baseline accumulated past slots** — Baseline cost calculation
  (optimizer.py `for s in range(n)`) counted all 96 slots without guarding by
  `start_slot`. Past slots inflated `baseline_cost` by up to ~44% over the
  remaining-day true baseline. Fixed by wrapping the accumulation in
  `if s >= start_slot:`.

- **Config UI fixes** — Various configuration flow and options flow corrections
  for field validation and defaults.

- **Issue #2 fix** — [details TBD]

- **Issue #1 fix** — Battery discharge safeguard edge case corrections.

- **Negative spot price VAT clamp** — When `spot < 0`, VAT multiplier is clamped
  to 1.0 so the subsidy passes through at face value without amplification.

- **Algorithm stability** — Solcast fallback, idle slot strategy edge cases, and
  SoC guard interval timing corrections.

### Changed

- **PV sell economic gate** — slots are only marked sell when the sell price
  exceeds the slot's buy price (`sell_price > buy_price`), in addition to the
  existing `pv_sell_min_price_spread` floor. Selling below the buy price loses
  money because a stored kWh displaces a future grid buy through the
  round-trip. Direct PV export incurs zero battery wear, so no wear-cost term
  is applied.

- **Cost sensor naming** — 4 cost/savings sensors prefixed with `Rest of Day`
  to clarify estimate scope (rest of today, not full day).

- **Emaldo schedule chart time scale** — Now includes tomorrow (day=1) slots
  from `coordinator.last_result_tomorrow` when available, matching
  `ScheduleChartSensor` time axis for aligned chart overlays.

- **Idle slot strategy** — Added `full_control`, `solar_guard`, `smart_override`
  strategies for controlling battery idle behaviour (initial commit).

- **Internal Nordpool sensor support** — Can read spot prices directly from
  Emaldo integration instead of requiring a separate price sensor.

- **Auto base load** — Recorder-based rolling 7-day average from household load
  sensor with ±50% clamp.

- **PV sell strategy** — Morning solar-to-grid arbitrage with configurable
  cutover time, solar margin guard, and min sell price threshold.

- **Control disable switch** — `switch.battery_optimizer_emaldo_control_enable`
  to test/dry-run without sending commands to the battery.

- **Third-party PV reconciliation** — After each optimizer recalculation, the
  Emaldo third-party PV switch is reconciled to the effective current plan.

- **Many refinements** — Round-trip trade pricing (efficiency + wear cost),
  supplier emission attribution, error suppression during HA startup phase,
  battery discharge safeguards, cloudy-day charging plan, Solcast p10/p50 mode.

