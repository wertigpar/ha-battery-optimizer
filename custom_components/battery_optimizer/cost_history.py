"""Pure helpers for realized grid cost history.

No Home Assistant imports — unit-tested directly (tests/test_cost_history.py).

Each 15-minute slot's realized cost is derived from metered import/export
energy counters and the slot's buy/sell prices:

    bill   = import_kwh * buy_price   (what the grid bills)
    refund = export_kwh * sell_price  (what the grid refunds for feed-in)
    net    = bill - refund            (signed: negative = net refund/profit)

The cost is signed so that a slot where feed-in exceeds purchase records a
negative value (a refund), and the day total sums correctly.
"""

from __future__ import annotations

from datetime import datetime, timedelta

SLOTS_PER_DAY = 96
SLOT_DURATION_HOURS = 0.25

# Rolling retention — 60 days at 96 slots/day = 5760 records.
_HISTORY_MAX_AGE_DAYS = 60
_HISTORY_MAX_RECORDS = SLOTS_PER_DAY * _HISTORY_MAX_AGE_DAYS


def local_slot_index(now: datetime) -> int:
    """Local 15-minute slot index (0..95) for a tz-aware datetime."""
    return min(SLOTS_PER_DAY - 1, now.hour * 4 + now.minute // 15)


def slot_cost(
    import_kwh: float, export_kwh: float, buy: float, sell: float
) -> dict[str, float]:
    """Cost of one 15-minute slot from metered import/export energy.

    Returns ``{bill, refund, net}`` rounded to 4 decimals. ``net`` is signed.
    """
    bill = round(import_kwh * buy, 4)
    refund = round(export_kwh * sell, 4)
    return {
        "bill": bill,
        "refund": refund,
        "net": round(bill - refund, 4),
    }


def prune_history(history: list[dict], now: datetime) -> list[dict]:
    """Drop records older than the retention window and cap the list."""
    cutoff = (now - timedelta(days=_HISTORY_MAX_AGE_DAYS)).isoformat()
    return [r for r in history if r.get("ts", "") >= cutoff][-_HISTORY_MAX_RECORDS:]


def today_records(history: list[dict] | None, today: datetime.date) -> list[dict]:
    """Records whose timestamp falls on ``today`` (local date)."""
    if not history:
        return []
    out: list[dict] = []
    for r in history:
        ts = r.get("ts")
        if not ts:
            continue
        try:
            # isoformat(timespec="seconds") → "2026-08-25T13:45:00[+02:00]"
            parsed = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            continue
        if parsed.date() == today:
            out.append(r)
    return out


def day_totals(records: list[dict]) -> dict[str, float]:
    """Sum bill/refund/net over a set of slot records."""
    net = sum(r.get("net", 0.0) for r in records)
    buy = sum(r.get("bill", 0.0) for r in records)
    sell = sum(r.get("refund", 0.0) for r in records)
    return {
        "net": round(net, 4),
        "buy": round(buy, 4),
        "sell": round(sell, 4),
    }


def compact_records(records: list[dict]) -> list[dict]:
    """Bounded, compact per-slot records for a sensor attribute.

    The full record set is far too heavy to serialize wholesale into a single
    HA sensor attribute: a full day's 96 slots (each with ts/slot/buy/sell/
    import_kwh/export_kwh/bill/refund/net) lands at ~15 KB, right at the
    recorder's 16384-byte per-attribute cap, so even a single duplicate slot
    drops the attribute (and its cost history) from long-term statistics.

    Returns a copy keeping only the keys the cost chart needs and capping the
    list to the most recent SLOTS_PER_DAY records, so the serialized attribute
    stays comfortably under the recorder cap on any day.
    """
    if not records:
        return []
    keep = (
        "ts",
        "slot",
        "buy",
        "sell",
        "import_kwh",
        "export_kwh",
        "net",
        "action",
        "soc_delta",
    )
    return [
        {k: r[k] for k in keep if k in r}
        for r in records[-SLOTS_PER_DAY:]
    ]


__all__ = [
    "SLOTS_PER_DAY",
    "SLOT_DURATION_HOURS",
    "local_slot_index",
    "slot_cost",
    "prune_history",
    "today_records",
    "day_totals",
    "compact_records",
]
