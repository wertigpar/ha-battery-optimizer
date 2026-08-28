"""Render a battery_optimizer run snapshot as LLM-readable markdown.

Pure module (no Home Assistant imports) — unit-tested directly.
"""
from __future__ import annotations


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def _time(slot: int) -> str:
    h, m = divmod(slot * 15, 60)
    return f"{h:02d}:{m:02d}"


def _table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(_fmt(c) for c in row) + " |")
    return "\n".join(out)


def render_analysis(snapshot: dict) -> str:
    """Render the snapshot as one markdown document."""
    lines: list[str] = []
    lines.append("# Battery Optimizer Run Analysis")
    lines.append("")

    state = snapshot.get("state", "planned")
    lines.append(f"- **State:** {state}")
    lines.append(f"- **Planned:** {snapshot.get('planned', False)}")
    lines.append(f"- **Version:** {snapshot.get('version', 'unknown')}")
    lines.append(f"- **Reason:** {snapshot.get('reason', '')}")
    lines.append(f"- **Written:** {snapshot.get('_written', '')}")
    lines.append(f"- **SoC input:** {snapshot.get('soc_input', '')}")
    lines.append(f"- **Start slot:** {snapshot.get('start_slot', '')}")
    lines.append("")

    if not snapshot.get("planned"):
        last_soc = snapshot.get("last_known_soc")
        lines.append("## Skipped run")
        lines.append("")
        lines.append(
            f"No schedule was produced. Last known SoC: "
            f"{'None' if last_soc is None else f'{last_soc:.1f}%'}."
        )
        lines.append("")
        return "\n".join(lines)

    cfg = snapshot.get("config", {})
    lines.append("## Configuration")
    lines.append("")
    lines.append(_table(["Key", "Value"], [[k, cfg[k]] for k in sorted(cfg)]))
    lines.append("")

    buy = snapshot.get("buy_prices", [])
    sell = snapshot.get("sell_prices", [])
    solar = snapshot.get("solar_15min", [])
    lines.append("## Effective inputs (96 slots)")
    lines.append("")
    rows = []
    for s in range(min(len(buy), len(sell), len(solar))):
        rows.append([
            s, _time(s),
            round(buy[s], 4), round(sell[s], 4),
            round(solar[s], 3),
        ])
    lines.append(_table(["slot", "time", "buy", "sell", "solar"], rows))
    lines.append("")

    plan = snapshot.get("plan", [])
    lines.append("## Planned schedule")
    lines.append("")
    p_rows = [
        [sp.get("index") or 0, _time(sp.get("index") or 0), sp.get("action"),
         sp.get("buy_price"), sp.get("sell_price"), sp.get("soc_after"),
         sp.get("profit")]
        for sp in plan
    ]
    lines.append(_table(
        ["slot", "time", "action", "buy", "sell", "soc_after", "profit"],
        p_rows,
    ))
    lines.append("")

    trace = snapshot.get("trace")
    lines.append("## Decision trace (gates and budgets)")
    lines.append("")
    if trace:
        lines.append(_table(["gate", "value"], [[k, trace[k]] for k in sorted(trace)]))
    else:
        lines.append("_no trace recorded_")
    lines.append("")

    outcome = snapshot.get("outcome", {})
    total_profit = outcome.get("total_profit", 0.0)
    baseline_cost = outcome.get("baseline_cost", 0.0)
    actual_cost = outcome.get("actual_cost")
    if actual_cost is None:
        actual_cost = round(baseline_cost - total_profit, 4)
    net_profit = outcome.get("net_profit", 0.0)
    wear_cost_total = outcome.get("wear_cost_total", 0.0)
    cycled_kwh = outcome.get("cycled_kwh", 0.0)
    n_charge = outcome.get("charge_slots", 0)
    n_discharge = outcome.get("discharge_slots", 0)
    n_idle = outcome.get("idle_slots", 0)
    n_plan = n_charge + n_discharge + n_idle
    capacity = cfg.get("capacity_kwh", 0.0)

    lines.append("## Cost breakdown")
    lines.append("")
    lines.append(
        "All three scenarios price solar the same way (self-consumption first): "
        "solar covering the load is neither imported nor sold; only the net "
        "deficit is imported and only the net surplus exported.  So the "
        "comparison is apples-to-apples."
    )
    lines.append("")
    lines.append(_table(
        ["metric", "value (€)", "meaning"],
        [
            ["baseline_cost", _fmt(baseline_cost),
             "No battery: grid imports only the net deficit, exports the net surplus"],
            ["optimized_cost", _fmt(baseline_cost - net_profit),
             "Optimizer plan total: grid cost + battery wear"],
            ["actual_cost", _fmt(actual_cost),
             "Optimizer plan grid cost only (excl. wear)"],
            ["wear_cost_total", _fmt(wear_cost_total),
             "Battery wear from cycled kWh"],
            ["gross_savings", _fmt(total_profit),
             "baseline_cost − actual_cost"],
            ["net_profit", _fmt(net_profit),
             "gross_savings − wear_cost_total"],
        ],
    ))
    lines.append("")

    # Option B — three costs side by side, no single net figure.
    emaldo_cost = snapshot.get("emaldo_plan_cost")
    internal = emaldo_cost if emaldo_cost is not None else None
    scenarios = []
    if internal is not None:
        scenarios.append(["Internal plan (Emaldo AI)", _fmt(internal),
                          "Device's own AI schedule, same inputs"])
    scenarios.append(["Baseline (no battery)", _fmt(baseline_cost),
                      "Grid imports only after sunset; surplus solar sold"])
    scenarios.append(["Optimizer plan", _fmt(actual_cost),
                      "Battery charged from solar; load covered by solar+battery"])
    lines.append("## Plan cost comparison")
    lines.append("")
    lines.append(_table(["Scenario", "Cost (€)", "What it does"], scenarios))
    lines.append("")
    lines.append(
        "**Reading the ordering:** the internal plan costs most (it imports "
        "from the grid even while the sun is up), the baseline is in the "
        "middle, and the optimizer is lowest.  The optimizer's slightly higher "
        "grid cost than the baseline is intentional — it means solar is being "
        "**stored in the battery** rather than sold, and that energy is "
        "discharged later when it displaces an evening grid import."
    )
    lines.append("")
    lines.append(f"- **Energy stored for later:** {_fmt(cycled_kwh)} kWh (cycled through the battery)")
    lines.append("")

    emaldo_plan = snapshot.get("emaldo_plan")
    emaldo_cost = snapshot.get("emaldo_plan_cost")
    if emaldo_plan is not None or emaldo_cost is not None:
        emaldo_grid = (emaldo_plan or {}).get("grid_cost")
        emaldo_wear = (emaldo_plan or {}).get("wear_cost")
        emaldo_cycled = (emaldo_plan or {}).get("cycled_kwh")
        emaldo_imp = (emaldo_plan or {}).get("import_kwh")
        emaldo_exp = (emaldo_plan or {}).get("export_kwh")
        lines.append("## Internal plan cost (Emaldo baseline)")
        lines.append("")
        lines.append(
            "The optimizer simulates what the device's own AI plan would have "
            "cost with the same inputs — the benchmark against which the "
            "optimizer's overrides are measured."
        )
        lines.append("")
        lines.append(_table(
            ["metric", "value"],
            [
                ["emaldo_plan_cost", _fmt(emaldo_cost)],
                ["emaldo_grid_cost", _fmt(emaldo_grid)],
                ["emaldo_wear_cost", _fmt(emaldo_wear)],
                ["emaldo_cycled_kwh", _fmt(emaldo_cycled)],
                ["emaldo_import_kwh", _fmt(emaldo_imp)],
                ["emaldo_export_kwh", _fmt(emaldo_exp)],
            ],
        ))
        lines.append("")

    lines.append("## Outcome")
    lines.append("")
    lines.append(_table(
        ["metric", "value"],
        [[k, outcome.get(k)] for k in
         ("total_profit", "baseline_cost", "net_profit", "wear_cost_total",
          "cycled_kwh", "charge_slots", "discharge_slots", "idle_slots")],
    ))
    lines.append("")
    lines.append(f"- **Push overrides:** {snapshot.get('push_overrides', 'unknown')}")
    lines.append("")

    lines.append("## AI evaluation")
    lines.append("")
    lines.append(
        "Grouped metrics for judging plan quality. Like is compared with "
        "like: money rows are in euros, percentages always name their "
        "denominator."
    )
    lines.append("")

    # Group 1 — monetary benchmarks: € compared with €, each percentage
    # states its own denominator so no cross-unit mixing happens.
    lines.append("### Benchmarks — money saved")
    lines.append("")
    money_rows = []
    if emaldo_plan is not None and emaldo_grid is not None and emaldo_grid > 0:
        savings = emaldo_grid - actual_cost
        money_rows.append([
            "savings_vs_emaldo", f"{savings:.4f} €",
            "Internal-plan grid cost minus optimizer grid cost (wear excluded on both sides); positive = optimizer cheaper than the device's own AI",
        ])
        money_rows.append([
            "improvement_over_emaldo", f"{savings / emaldo_grid * 100:.1f}%",
            "The euro saving above, divided by the internal-plan cost; can exceed 100% when the plan turns a net profit",
        ])
    lines.append(_table(["metric", "value", "meaning"], money_rows))
    lines.append("")

    # Group 1b — the meaningful comparison: how the three scenarios rank
    # against each other.  No single "net savings" figure is shown, because
    # the optimizer deliberately banks solar in the battery instead of
    # selling it, so its grid cost can sit slightly below baseline without
    # that being a real win on this window.
    lines.append("### Cost ordering")
    lines.append("")
    order_rows = []
    if emaldo_cost is not None:
        order_rows.append(["Internal plan", f"{emaldo_cost:.4f} €",
                           "Highest — imports from the grid even while the sun is up"])
    order_rows.append(["Baseline (no battery)", f"{baseline_cost:.4f} €",
                       "Middle — grid imports only after sunset; surplus solar sold"])
    order_rows.append(["Optimizer plan", f"{actual_cost:.4f} €",
                       "Lowest — battery charged from solar; load covered by solar+battery"])
    lines.append(_table(["Scenario", "Cost", "Why"], order_rows))
    lines.append("")
    lines.append(
        "The optimizer's grid cost can sit slightly below the baseline on a "
        "purely sunny day because it is **storing solar in the battery** rather "
        "than selling it — that energy is discharged later and displaces an "
        "evening grid import.  A small gap vs baseline on this window is "
        "intended banked energy, not an error."
    )
    lines.append("")

    # Group 2 — battery usage: energy numbers compared with energy limits.
    lines.append("### Battery usage")
    lines.append("")
    usage_rows = [
        ["cycled_kwh", _fmt(cycled_kwh),
         "Battery-internal energy discharged under this plan"],
    ]
    if capacity > 0:
        usage_rows.append([
            "throughput_of_capacity", f"{cycled_kwh / capacity * 100:.1f}%",
            "cycled_kwh divided by capacity_kwh",
        ])
    budget = trace.get("total_discharge_budget") if trace else None
    if isinstance(budget, (int, float)) and budget > 0:
        usage_rows.append([
            "budget_used", f"{min(cycled_kwh / budget, 9.99) * 100:.1f}%",
            "cycled_kwh divided by total_discharge_budget; low = price-limited, near 100% = energy-limited",
        ])
    lines.append(_table(["metric", "value", "meaning"], usage_rows))
    lines.append("")

    # Group 3 — slot allocation: counts of one window, shares of that same count.
    lines.append("### Slot allocation (planned window)")
    lines.append("")
    alloc_rows = []
    if n_plan:
        for label, cnt in (("charge", n_charge),
                           ("discharge", n_discharge),
                           ("idle", n_idle)):
            alloc_rows.append([
                label, str(cnt), f"{cnt / n_plan * 100:.1f}% of {n_plan} planned slots",
            ])
    else:
        alloc_rows.append(["none", "0", "—"])
    lines.append(_table(["action", "slots", "share"], alloc_rows))
    lines.append("")
    return "\n".join(lines)