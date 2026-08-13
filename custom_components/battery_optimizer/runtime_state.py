"""Last-run runtime state — serialize, validate, rebuild across restarts.

Pure module (no Home Assistant imports): standalone-loadable via importlib,
no relative imports, no dependency on the ``bopt_pkg`` package fabrication.

Semantics
---------
Slots are day-relative (0-95, one per 15-minute block of a single day) and
the snapshot is a daily counter (cumulative meter values for that day).
Persisted runtime state is therefore only meaningful on the same calendar
day it was written: slot indices and snapshot deltas line up with the
planner's day-relative model only while that day is still the current one.
:func:`rebuild_runtime` enforces this by comparing the persisted
``last_run`` date against the caller's ``now`` date; stale-day state
(written any other day) is discarded — ``rebuild_runtime`` returns ``None``.
"""

from __future__ import annotations

from datetime import datetime, date

_MISSING = object()


def _is_number(value) -> bool:
    """True for real numbers; booleans are ints in Python and must NOT pass."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def prune_plan_slots(slots, from_slot: int) -> list[tuple]:
    """Subset SlotPlan-like objects into [index, action, soc_after, solar_kw] tuples.

    Drops entries with ``obj.index < from_slot``. Input objects expose
    ``.index`` (int), ``.action`` (str), ``.soc_after`` (float), ``.solar_kw``
    (float). Returns list of 4-tuples in input order.
    """
    result = []
    for obj in slots:
        index = getattr(obj, "index", _MISSING)
        action = getattr(obj, "action", _MISSING)
        soc_after = getattr(obj, "soc_after", _MISSING)
        solar_kw = getattr(obj, "solar_kw", _MISSING)
        if _MISSING in (index, action, soc_after, solar_kw):
            continue
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        if index < from_slot:
            continue
        result.append((index, action, soc_after, solar_kw))
    return result


def serialize_runtime(
    *,
    last_run_slot: int,
    last_run_initial_soc: float,
    last_run_scale: float,
    last_run_ts: str,
    snapshot: dict,
    plan_slots: list,
) -> dict:
    """JSON-able sidecar payload for the last optimizer run.

    ``plan_slots`` is the output of :func:`prune_plan_slots` (list of
    4-tuples). ``last_run_ts`` is the run time as ISO string. ``snapshot`` is
    copied (not referenced).
    """
    return {
        "last_run_slot": last_run_slot,
        "last_run_initial_soc": last_run_initial_soc,
        "last_run_scale": last_run_scale,
        "last_run": last_run_ts,
        "snapshot": dict(snapshot),
        "plan_slots": [list(entry) for entry in plan_slots],
    }


def rebuild_runtime(data, now: datetime) -> dict | None:
    """Validate persisted runtime state; None when unusable.

    Returns None for: non-dict data, any missing/invalid required key,
    unparseable ``last_run`` ISO, ``last_run`` date differing from
    ``now.date()`` (stale day — slots are day-relative), empty plan_slots,
    or any plan entry that is not a list/tuple of 4 with
    [int index, str action, number soc_after, number solar_kw]
    (booleans rejected via _is_number).
    """
    if not isinstance(data, dict):
        return None

    last_run_slot = data.get("last_run_slot")
    if not isinstance(last_run_slot, int) or isinstance(last_run_slot, bool):
        return None
    if last_run_slot < 0:
        return None

    initial_soc = data.get("last_run_initial_soc")
    if not _is_number(initial_soc):
        return None

    scale = data.get("last_run_scale")
    if not _is_number(scale):
        return None

    last_run = data.get("last_run")
    if not isinstance(last_run, str):
        return None
    try:
        parsed = datetime.fromisoformat(last_run)
    except ValueError:
        return None
    if parsed.date() != now.date():
        return None

    snapshot = data.get("snapshot")
    if not isinstance(snapshot, dict):
        return None

    plan_slots = data.get("plan_slots")
    if not isinstance(plan_slots, list) or not plan_slots:
        return None

    normalized = []
    for entry in plan_slots:
        if not isinstance(entry, (list, tuple)) or len(entry) != 4:
            return None
        index, action, soc_after, solar_kw = entry
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            return None
        if not isinstance(action, str):
            return None
        if not _is_number(soc_after) or not _is_number(solar_kw):
            return None
        normalized.append((index, action, float(soc_after), float(solar_kw)))

    return {
        "last_run_slot": last_run_slot,
        "last_run_initial_soc": initial_soc,
        "last_run_scale": scale,
        "last_run": last_run,
        "snapshot": snapshot,
        "plan_slots": normalized,
    }
