"""Override push bookkeeping and the enforcement decisions (issue #36).

Pure module (no Home Assistant imports, no relative imports) - unit-tested
directly in ``tests/test_push_state.py``.

The Emaldo ``apply_bulk_schedule`` service returns nothing on failure, so the
coordinator cannot tell a dropped push from an accepted one by the call
alone.  Two independent signals cover that gap:

1. *Time and outcome bookkeeping* (:func:`decide_reassert`): the unchanged-plan
   checkpoint re-sends the override when the last push failed recently, when
   no push has ever succeeded, when the last successful push is old, or when
   the planned-vs-actual discharge shows a large shortfall.
2. *Device read-back* (:func:`current_slot_source`, :func:`decide_enforcement`):
   the Emaldo schedule chart reports, per slot, whether the device is running
   its internal AI schedule or an override.  If an override was sent for the
   current slot and the device reports the slot as internal, enforcement has
   been lost and the override is re-pushed on the next 5-minute tick rather
   than at the next checkpoint up to two hours later.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

#: Emaldo's "follow the base schedule" byte; mirrors const.SLOT_NO_OVERRIDE.
SLOT_NO_OVERRIDE = 0x80

#: :func:`decide_enforcement` outcomes.
ENFORCEMENT_NONE_SENT = "none_sent"        # nothing to enforce in this slot
ENFORCEMENT_UNVERIFIABLE = "unverifiable"  # device gives no usable source
ENFORCEMENT_CONFIRMED = "confirmed"        # device reports the override
ENFORCEMENT_LOST = "lost"                  # device reports its own schedule

#: ``source`` values the Emaldo chart uses for an enforced override slot and
#: for a slot the device decides itself.  Matched as substrings, lowercased,
#: so a wording change on the ha-emaldo side degrades to *unverifiable*
#: (no re-push) rather than to a false *lost* that would re-push every tick.
_OVERRIDE_SOURCES = ("override", "e2e", "optimizer")
_INTERNAL_SOURCES = ("internal", "ai", "base", "default", "schedule")


def age_seconds(now: datetime, then: datetime) -> float:
    """Seconds from ``then`` to ``now``; tolerates one naive side.

    A naive ``then`` (an old marker written without an offset) is compared as
    wall-clock time against an aware ``now``, which is what the coordinator's
    own ``dt_util.now()`` would have produced for it.
    """
    if now.tzinfo is None and then.tzinfo is not None:
        then = then.replace(tzinfo=None)
    elif now.tzinfo is not None and then.tzinfo is None:
        now = now.replace(tzinfo=None)
    return (now - then).total_seconds()


def decide_reassert(
    *,
    now: datetime,
    last_push_at: datetime | None,
    last_push_ok_at: datetime | None,
    last_push_fail_at: datetime | None,
    fail_streak: int,
    accuracy: dict[str, Any] | None,
    push_fail_window_s: float = 600.0,
    push_settle_s: float = 300.0,
    stale_after_s: float = 1800.0,
    accuracy_cooldown_s: float = 900.0,
    accuracy_shortfall_kwh: float = 0.35,
    accuracy_ratio: float = 0.25,
) -> tuple[bool, str | None]:
    """Whether an unchanged plan should be re-pushed anyway, and why.

    Returns ``(True, reason)`` with reason one of ``push_failed_recently``,
    ``no_successful_push``, ``stale_push`` or ``accuracy_divergence``, else
    ``(False, None)``.

    ``accuracy`` is the coordinator's plan-accuracy record (keys
    ``elapsed_slots``, ``planned_discharge_kwh``, ``actual_discharge_kwh``).
    """
    if fail_streak > 0 and last_push_fail_at is not None:
        if age_seconds(now, last_push_fail_at) < push_fail_window_s:
            return True, "push_failed_recently"
    if last_push_ok_at is None:
        # A push went out moments ago and has not had time to be judged:
        # do not stack a second one on top of it.
        if last_push_at is not None and age_seconds(now, last_push_at) < push_settle_s:
            return False, None
        return True, "no_successful_push"
    age = age_seconds(now, last_push_ok_at)
    if age >= stale_after_s:
        return True, "stale_push"
    if accuracy and accuracy.get("elapsed_slots"):
        planned = float(accuracy.get("planned_discharge_kwh") or 0.0)
        actual = accuracy.get("actual_discharge_kwh")
        # Cooldown: a fresh push needs time to show up in the actuals, so do
        # not re-push on every checkpoint while the shortfall persists.
        if (
            age >= accuracy_cooldown_s
            and planned >= accuracy_shortfall_kwh
            and actual is not None
            and float(actual) < planned * accuracy_ratio
        ):
            return True, "accuracy_divergence"
    return False, None


def current_slot_source(
    schedule: Any, now: datetime, slot_minutes: int = 15
) -> str | None:
    """The Emaldo chart's ``source`` for the slot that contains ``now``.

    ``schedule`` is the chart sensor's ``schedule`` attribute: a list of
    per-slot dicts with an ISO ``t`` start and, on current ha-emaldo
    versions, a ``source`` string (``internal`` or ``override``).  Returns
    the source lowercased and stripped, or None when the attribute is
    missing, malformed, has no slot for ``now`` or carries no source.
    """
    if not isinstance(schedule, list):
        return None
    width = timedelta(minutes=slot_minutes)
    for slot in schedule:
        if not isinstance(slot, dict):
            continue
        try:
            start = datetime.fromisoformat(slot["t"])
        except (KeyError, TypeError, ValueError):
            continue
        if start.tzinfo is None and now.tzinfo is not None:
            start = start.replace(tzinfo=now.tzinfo)
        elif start.tzinfo is not None and now.tzinfo is None:
            start = start.replace(tzinfo=None)
        if start <= now < start + width:
            source = slot.get("source")
            if isinstance(source, str) and source.strip():
                return source.strip().lower()
            return None
    return None


def decide_enforcement(
    *, sent_value: int | None, source: str | None
) -> str:
    """Classify the device's read-back against what was pushed for the slot.

    ``sent_value`` is the byte the coordinator last sent for the current slot
    (None when nothing was sent); ``source`` is :func:`current_slot_source`.
    """
    if sent_value is None or sent_value == SLOT_NO_OVERRIDE:
        return ENFORCEMENT_NONE_SENT
    if source is None:
        return ENFORCEMENT_UNVERIFIABLE
    if any(key in source for key in _OVERRIDE_SOURCES):
        return ENFORCEMENT_CONFIRMED
    if any(key in source for key in _INTERNAL_SOURCES):
        return ENFORCEMENT_LOST
    return ENFORCEMENT_UNVERIFIABLE


__all__ = [
    "ENFORCEMENT_CONFIRMED",
    "ENFORCEMENT_LOST",
    "ENFORCEMENT_NONE_SENT",
    "ENFORCEMENT_UNVERIFIABLE",
    "SLOT_NO_OVERRIDE",
    "age_seconds",
    "current_slot_source",
    "decide_enforcement",
    "decide_reassert",
]
