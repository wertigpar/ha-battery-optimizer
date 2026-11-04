"""plan_export renderer tests (pure)."""
from custom_components.battery_optimizer.plan_export import render_analysis


def _full_snapshot() -> dict:
    return {
        "state": "planned", "planned": True, "version": "0.3.7",
        "reason": "checkpoint", "_written": "2026-08-26T19:00:00", "soc_input": "read",
        "start_slot": 76,
        "config": {"capacity_kwh": 15.0, "soc_min": 20.0},
        "buy_prices": [0.10] * 96,
        "sell_prices": [0.01] * 96,
        "solar_15min": [0.0] * 96,
        "plan": [
            {"index": 76, "action": "discharge", "buy_price": 0.30,
             "sell_price": 0.01, "soc_after": 95.0, "profit": 0.05},
            {"index": 88, "action": "idle", "buy_price": 0.20,
             "sell_price": 0.01, "soc_after": 94.0, "profit": 0.0},
        ],
        "trace": {"initial_soc_pct": 100.0, "case_a_floor": 0.033,
                  "total_discharge_budget": 11.25, "mode": "combined"},
        "outcome": {"total_profit": 0.5, "baseline_cost": 2.0,
                    "wear_cost_total": 0.1, "cycled_kwh": 1.5,
                    "net_profit": 0.4, "charge_slots": 0,
                    "discharge_slots": 1, "idle_slots": 95},
        "push_overrides": 14,
        "emaldo_plan_cost": 1.8,
        "emaldo_plan": {"grid_cost": 1.7, "wear_cost": 0.1, "cycled_kwh": 1.0,
                        "import_kwh": 10.0, "export_kwh": 0.0},
    }


def test_full_snapshot_has_all_sections():
    md = render_analysis(_full_snapshot())
    for header in ("# Battery Optimizer Run Analysis", "## Configuration",
                   "## Effective inputs", "## Planned schedule",
                   "## Decision trace", "## Outcome"):
        assert header in md


def test_plan_rows_rendered():
    md = render_analysis(_full_snapshot())
    assert "discharge" in md and "idle" in md
    assert "19:00" in md   # slot 76 -> 19:00
    assert "22:00" in md   # slot 88 -> 22:00


def test_trace_numbers_rendered():
    md = render_analysis(_full_snapshot())
    assert "total_discharge_budget" in md and "11.25" in md
    assert "case_a_floor" in md and "0.033" in md


def test_skipped_snapshot_renders():
    md = render_analysis({
        "state": "soc_guard_skip", "planned": False, "reason": "checkpoint",
        "last_known_soc": None,
    })
    assert "Skipped run" in md
    assert "No schedule was produced" in md


def test_missing_snapshot_is_tolerated():
    md = render_analysis({})
    assert "# Battery Optimizer Run Analysis" in md


def test_cost_breakdown_renders():
    md = render_analysis(_full_snapshot())
    assert "## Cost breakdown" in md
    for key in ("baseline_cost", "optimized_cost", "actual_cost",
                "wear_cost_total", "gross_savings", "net_profit"):
        assert key in md


def test_derived_actual_cost_fallback():
    """actual_cost absent -> derived from baseline_cost - total_profit."""
    md = render_analysis(_full_snapshot())
    # baseline 2.0 - total_profit 0.5 = 1.5
    assert "1.5" in md
    # net_profit 0.4 -> optimized_cost = baseline - net_profit = 1.6
    assert "1.6" in md


def test_emaldo_plan_section_renders():
    md = render_analysis(_full_snapshot())
    assert "## Internal plan cost (Emaldo baseline)" in md
    assert "emaldo_plan_cost" in md
    assert "1.8" in md     # emaldo_plan_cost
    assert "1.7" in md     # emaldo_grid_cost


def test_ai_evaluation_section_renders():
    md = render_analysis(_full_snapshot())
    assert "## AI evaluation" in md
    # grouped, like-for-like blocks
    assert "### Benchmarks — money saved" in md
    assert "### Cost ordering" in md
    assert "### Battery usage" in md
    assert "### Slot allocation (planned window)" in md
    assert "savings_vs_emaldo" in md
    assert "cycled_kwh" in md
    # every euro figure carries a unit; every percentage names denominator
    # (no bare % vs € rows mixed in one comparison list)
    for block in md.split("### ")[1:]:
        for line in block.splitlines():
            if line.startswith("|") and not line.startswith("| metric") \
                    and not line.startswith("| action") \
                    and not line.startswith("| Scenario"):
                cells = [c for c in line.split("|")[1:-1]]
                assert len(cells) == 3, f"row has {len(cells)} cells: {line}"


def test_ai_evaluation_denominators_named():
    md = render_analysis(_full_snapshot())
    assert "improvement_over_emaldo" in md
    assert "divided by the internal-plan cost" in md


def test_budget_used_row_when_trace_has_budget():
    snap = _full_snapshot()
    snap["trace"]["total_discharge_budget"] = 10.0
    md = render_analysis(snap)
    assert "budget_used" in md
    # cycled 1.5 of budget 10.0 -> 15.0%
    assert "15.0%" in md


def test_plan_cost_comparison_renders():
    md = render_analysis(_full_snapshot())
    assert "## Plan cost comparison" in md
    assert "### Cost ordering" in md
    assert "Internal plan" in md
    assert "Baseline (no battery)" in md
    assert "Optimizer plan" in md


def test_plan_cost_comparison_absent_when_no_outcome():
    md = render_analysis(_full_snapshot())
    assert "Plan cost comparison" in md