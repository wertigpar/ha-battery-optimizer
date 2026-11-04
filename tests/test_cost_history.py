"""Tests for the realized cost-history helper (pure, no HA dependency)."""

from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.battery_optimizer.cost_history import (
    SLOTS_PER_DAY,
    compact_records,
    day_totals,
    local_slot_index,
    prune_history,
    slot_cost,
    today_records,
)


def test_local_slot_index_boundaries():
    assert local_slot_index(datetime(2026, 1, 1, 0, 0)) == 0
    assert local_slot_index(datetime(2026, 1, 1, 0, 14)) == 0
    assert local_slot_index(datetime(2026, 1, 1, 0, 15)) == 1
    assert local_slot_index(datetime(2026, 1, 1, 12, 30)) == (12 * 60 + 30) // 15
    assert local_slot_index(datetime(2026, 1, 1, 23, 45)) == SLOTS_PER_DAY - 1


def test_slot_cost_sign_and_math():
    # 1 kWh import @ 0.10 buy => bill 0.10; 2 kWh export @ 0.05 sell => refund 0.10
    cost = slot_cost(1.0, 2.0, 0.10, 0.05)
    assert cost == {"bill": 0.10, "refund": 0.10, "net": 0.0}
    # Pure import => positive net (cost).
    cost = slot_cost(2.0, 0.0, 0.20, 0.05)
    assert cost["net"] == 0.40
    # Pure export => negative net (refund).
    cost = slot_cost(0.0, 4.0, 0.20, 0.06)
    assert cost["net"] == -0.24


def test_prune_history_drops_old_records():
    now = datetime(2026, 6, 1, 12, 0)
    old = (now - timedelta(days=61)).isoformat(timespec="seconds")
    recent = (now - timedelta(days=1)).isoformat(timespec="seconds")
    history = [
        {"ts": old, "slot": 0, "bill": 0.1, "refund": 0.0, "net": 0.1},
        {"ts": recent, "slot": 1, "bill": 0.2, "refund": 0.0, "net": 0.2},
    ]
    pruned = prune_history(history, now)
    assert len(pruned) == 1
    assert pruned[0]["slot"] == 1


def test_today_records_filters_by_date():
    today = datetime(2026, 6, 1).date()
    history = [
        {"ts": "2026-06-01T00:00:00", "slot": 0, "bill": 0.1, "refund": 0.0, "net": 0.1},
        {"ts": "2026-06-01T12:00:00", "slot": 48, "bill": 0.3, "refund": 0.1, "net": 0.2},
        {"ts": "2026-05-31T23:00:00", "slot": 95, "bill": 0.5, "refund": 0.0, "net": 0.5},
    ]
    recs = today_records(history, today)
    assert len(recs) == 2
    assert all(r["ts"].startswith("2026-06-01") for r in recs)


def test_day_totals_sums_components():
    records = [
        {"ts": "2026-06-01T00:00:00", "slot": 0, "bill": 0.1, "refund": 0.0, "net": 0.1},
        {"ts": "2026-06-01T12:00:00", "slot": 48, "bill": 0.3, "refund": 0.1, "net": 0.2},
    ]
    totals = day_totals(records)
    assert totals == {"net": 0.3, "buy": 0.4, "sell": 0.1}


def test_day_totals_empty():
    assert day_totals([]) == {"net": 0.0, "buy": 0.0, "sell": 0.0}


def _slot_rec(idx):
    return {
        "ts": f"2026-06-01T{idx // 4:02d}:{(idx % 4) * 15:02d}:00",
        "slot": idx,
        "buy": 0.10,
        "sell": 0.05,
        "import_kwh": 0.5,
        "export_kwh": 0.0,
        "bill": 0.05,
        "refund": 0.0,
        "net": 0.05,
        "action": "charge",
        "soc": 50.0,
        "soc_delta": 1.5,
    }


def test_compact_records_drops_derived_keys():
    records = [_slot_rec(0), _slot_rec(1)]
    compact = compact_records(records)
    assert len(compact) == 2
    for rec in compact:
        assert set(rec) == {
            "ts", "slot", "buy", "sell",
            "import_kwh", "export_kwh", "net",
        }
        assert "bill" not in rec
        assert "refund" not in rec
        assert "soc" not in rec
        assert "action" not in rec
        assert "soc_delta" not in rec


def test_compact_records_empty():
    assert compact_records([]) == []


def test_compact_records_bounded_to_a_day():
    records = [_slot_rec(i) for i in range(SLOTS_PER_DAY + 10)]
    compact = compact_records(records)
    assert len(compact) == SLOTS_PER_DAY
    # Keeps the most recent (highest-slot) records.
    assert compact[-1]["slot"] == SLOTS_PER_DAY + 9


def test_compact_records_serializes_under_recorder_cap():
    # A full day's 96 records, compacted, must stay well under the recorder's
    # 16384-byte per-attribute cap when serialized.
    import json

    full = compact_records([_slot_rec(i) for i in range(SLOTS_PER_DAY)])
    assert len(json.dumps(full).encode("utf-8")) < 16384
