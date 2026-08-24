"""User schedule rules — pure module, no Home Assistant imports.

Rule model, validation, per-day expansion (ladder walk) and byte masking
for the user schedule layer.  Rules are configured as config subentries;
this module is the pure logic layer so it can be unit-tested standalone
(via the fabricated ``bopt_pkg`` loader in tests/__init__.py) without HA.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .const import (
    SLOTS_PER_DAY,
    SLOT_NO_OVERRIDE,
    SLOT_IDLE,
    DEFAULT_RULE_LABEL,
)

# ── Levels (strongest first — ladder walk order) ─────────────────────
LEVEL_DATE = "date"
LEVEL_WEEKDAY = "weekday"
LEVEL_DEFAULT = "default"
LEVELS_STRONGEST_FIRST = (LEVEL_DATE, LEVEL_WEEKDAY, LEVEL_DEFAULT)

# ── Actions ──────────────────────────────────────────────────────────
ACTIONS = ("charge", "idle", "discharge", "original", "optimizer")
ACTION_CHARGE = "charge"
ACTION_IDLE = "idle"
ACTION_DISCHARGE = "discharge"
ACTION_ORIGINAL = "original"
ACTION_OPTIMIZER = "optimizer"

# ── PV behavior ──────────────────────────────────────────────────────
PV_INHERIT = "inherit"
PV_SELL = "sell"
PV_CHARGE = "charge"
PV_BEHAVIORS = (PV_INHERIT, PV_SELL, PV_CHARGE)

SLOT_MINUTES = 15


@dataclass
class UserRule:
    """One user-defined schedule rule."""

    level: str = LEVEL_DEFAULT
    days: list[int] = field(default_factory=list)   # weekday: 0=Mon..6=Sun
    start_date: str | None = None                    # "YYYY-MM-DD", date level
    end_date: str | None = None                      # "YYYY-MM-DD", date level
    start_time: str = "00:00"                        # "HH:MM", inclusive
    end_time: str = "24:00"                          # "HH:MM", exclusive
    action: str = ACTION_OPTIMIZER
    soc_target: int | None = None
    pv_sell: str = PV_INHERIT
    label: str = ""
    enabled: bool = True


@dataclass
class SlotWinner:
    """Winning rule decision for one 15-min slot."""

    action: str
    soc_target: int | None = None
    pv_sell: str = PV_INHERIT
    level: str | None = None
    rule_index: int | None = None


WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _weekday_range_summary(days: list[int]) -> str:
    """Group weekday ints into ranges: [0,1,2,4,5] -> 'Mon–Wed, Fri–Sat'.

    Full week (all seven days) -> 'Every day'.  Empty -> 'Weekday'.
    Sat (5) and Sun (6) never merge into a range — the weekend pair
    always renders as two names ("Sat, Sun").
    """
    if not days:
        return "Weekday"
    if len(days) == 7 and set(days) == set(range(7)):
        return "Every day"
    ordered = sorted(days)
    parts: list[str] = []
    run_start = ordered[0]
    prev = ordered[0]
    for day in ordered[1:]:
        if day == prev + 1 and prev != 5:
            prev = day
            continue
        if run_start == prev:
            parts.append(WEEKDAY_NAMES[run_start])
        else:
            parts.append(f"{WEEKDAY_NAMES[run_start]}–{WEEKDAY_NAMES[prev]}")
        run_start = prev = day
    if run_start == prev:
        parts.append(WEEKDAY_NAMES[run_start])
    else:
        parts.append(f"{WEEKDAY_NAMES[run_start]}–{WEEKDAY_NAMES[prev]}")
    return ", ".join(parts)


def _action_label(rule: UserRule) -> str:
    if rule.action == ACTION_CHARGE:
        target = rule.soc_target if rule.soc_target is not None else "?"
        return f"Charge to {target}%"
    if rule.action == ACTION_DISCHARGE:
        target = rule.soc_target if rule.soc_target is not None else "?"
        return f"Discharge to {target}%"
    return {
        ACTION_IDLE: "Idle",
        ACTION_ORIGINAL: "Original",
        ACTION_OPTIMIZER: "Optimizer",
    }.get(rule.action, rule.action)


def rule_summary(rule: UserRule) -> str:
    """One-line human summary of a rule for the config list / flow titles.

    Default rules show no days and omit the time part when they cover the
    full day; all other levels always show days and time.
    """
    action = _action_label(rule)

    if rule.level == LEVEL_DEFAULT:
        if rule.start_time == "00:00" and rule.end_time == "24:00":
            return action
        return f"{action} · {rule.start_time}–{rule.end_time}"

    if rule.level == LEVEL_DATE:
        days = rule.start_date or "Every day"
        if rule.end_date and rule.end_date != rule.start_date:
            days = f"{rule.start_date}–{rule.end_date}"
    else:
        days = _weekday_range_summary(rule.days)

    return f"{action} · {days} · {rule.start_time}–{rule.end_time}"


def default_rule_title(rule: UserRule) -> str:
    """Subentry title for the default rule, showing its action.

    e.g. "Default Schedule (Optimizer)", "Default Schedule (Original)",
    "Default Schedule (Charge to 90%)".  Keeps the stable label so the
    default rule stays identifiable, while surfacing which source
    (optimizer / battery AI / manual action) actually governs.
    """
    summary = rule_summary(rule)
    if summary == "Optimizer":
        return f"{DEFAULT_RULE_LABEL} (Optimizer)"
    return f"{DEFAULT_RULE_LABEL} ({summary})"


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    """Parse 'HH:MM' -> (hours, minutes); None when malformed."""
    try:
        h, m = value.split(":")
        hours, minutes = int(h), int(m)
    except (ValueError, AttributeError):
        return None
    if hours < 0 or hours > 24 or minutes < 0 or minutes > 59:
        return None
    if hours == 24 and minutes != 0:
        return None
    return hours, minutes


def _minutes(value: str) -> int | None:
    """'HH:MM' -> minutes since midnight (24:00 -> 1440)."""
    parsed = _parse_hhmm(value)
    if parsed is None:
        return None
    h, m = parsed
    return h * 60 + m


def _window_mins(rule: UserRule) -> tuple[int | None, int | None]:
    return _minutes(rule.start_time), _minutes(rule.end_time)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def rule_from_data(data: dict) -> UserRule:
    """Build a UserRule from subentry data (JSON-serializable dict).

    Single converter shared by the config flow and the coordinator —
    empty strings for dates become None.
    """
    return UserRule(
        level=data.get("level", LEVEL_DEFAULT),
        days=list(data.get("days") or []),
        start_date=data.get("start_date") or None,
        end_date=data.get("end_date") or None,
        start_time=data.get("start_time", "00:00"),
        end_time=data.get("end_time", "24:00"),
        action=data.get("action", ACTION_OPTIMIZER),
        soc_target=data.get("soc_target"),
        pv_sell=data.get("pv_sell", PV_INHERIT),
        label=data.get("label", ""),
        enabled=bool(data.get("enabled", True)),
    )


def rule_errors(rule: UserRule, siblings: list[UserRule]) -> list[str]:
    """Validate a rule against itself and same-level siblings.

    ``siblings`` must contain only same-level rules, with the rule being
    edited already excluded.  Returns a list of human-readable errors;
    empty list means valid.
    """
    errors: list[str] = []

    if rule.action not in ACTIONS:
        errors.append(f"Unknown action '{rule.action}'")
    if rule.pv_sell not in PV_BEHAVIORS:
        errors.append(f"Unknown PV behavior '{rule.pv_sell}'")

    start_m, end_m = _window_mins(rule)
    if start_m is None or end_m is None:
        errors.append("Invalid time format (use HH:MM)")
    else:
        if start_m % SLOT_MINUTES != 0 or end_m % SLOT_MINUTES != 0:
            errors.append("Times must be 15-minute aligned")
        if rule.level in (LEVEL_WEEKDAY, LEVEL_DEFAULT) and end_m <= start_m:
            errors.append("Weekday/default rules cannot cross midnight")
        if rule.level == LEVEL_DATE and end_m == start_m:
            errors.append("Rule window must not be empty")

    if rule.action in (ACTION_CHARGE, ACTION_DISCHARGE):
        if rule.soc_target is None or not 1 <= rule.soc_target <= 100:
            errors.append("SoC target must be between 1 and 100")

    if rule.level == LEVEL_WEEKDAY:
        if not rule.days:
            errors.append("Weekday rule needs at least one day")
        if any(not isinstance(d, int) or d < 0 or d > 6 for d in rule.days):
            errors.append("Weekday out of range (0=Mon..6=Sun)")
    elif rule.level == LEVEL_DATE:
        d0 = _parse_date(rule.start_date)
        d1 = _parse_date(rule.end_date) if rule.end_date else d0
        if d0 is None:
            errors.append("Date rule needs a start date")
        elif d1 is not None and d1 < d0:
            errors.append("End date before start date")
        if start_m is not None and end_m is not None and end_m < start_m:
            # overnight (end < start) requires a multi-day range
            d_end = _parse_date(rule.end_date) if rule.end_date else None
            if d_end is None or d_end == d0:
                errors.append("Overnight date rules need an end date after the start date")

    # Same-level overlap check
    for other in siblings:
        if _overlaps(rule, other):
            other_label = other.label or f"{other.level} {other.start_time}-{other.end_time}"
            errors.append(f"Overlaps existing rule '{other_label}'")

    return errors


def _overlaps(a: UserRule, b: UserRule) -> bool:
    """True when two same-level rules share any slot on any day."""
    if a.level != b.level:
        return False
    a_start, a_end = _window_mins(a)
    b_start, b_end = _window_mins(b)
    if a_start is None or a_end is None or b_start is None or b_end is None:
        return False
    window_overlap = a_start < b_end and b_start < a_end

    if a.level == LEVEL_WEEKDAY:
        return window_overlap and bool(set(a.days) & set(b.days))
    if a.level == LEVEL_DEFAULT:
        return window_overlap
    # date level — compare per calendar day
    a0 = _parse_date(a.start_date)
    b0 = _parse_date(b.start_date)
    if a0 is None or b0 is None:
        return False
    a1 = _parse_date(a.end_date) or a0
    b1 = _parse_date(b.end_date) or b0
    if a1 < b0 or b1 < a0:
        return False
    lo, hi = max(a0, b0), min(a1, b1)
    day = lo
    while day <= hi:
        if _day_window_overlaps(a, day, b, day):
            return True
        day += timedelta(days=1)
    return False


def _day_window_overlaps(a: UserRule, day_a: date, b: UserRule, day_b: date) -> bool:
    """Do two date-level rules share a slot on the given day?  Handles
    overnight windows (end < start) via per-day segments."""
    segs_a = _date_rule_segments(a, day_a)
    segs_b = _date_rule_segments(b, day_b)
    for sa in segs_a:
        for sb in segs_b:
            if sa[0] < sb[1] and sb[0] < sa[1]:
                return True
    return False


def _date_rule_segments(rule: UserRule, day: date) -> list[tuple[int, int]]:
    """[(start_min, end_min)] covered by a date rule on ``day``.

    Same-day window (end > start): [start, end) on every day in range.
    Overnight (end < start): [start, 1440) on day == start_date,
    [0, end) on day == end_date, full day on middle days.
    """
    d0 = _parse_date(rule.start_date)
    if d0 is None:
        return []
    d1 = _parse_date(rule.end_date) or d0
    if day < d0 or day > d1:
        return []
    start_m, end_m = _window_mins(rule)
    if start_m is None or end_m is None:
        return []
    if end_m > start_m:
        return [(start_m, end_m)]
    # overnight (multi-day only — single-day rejected in rule_errors)
    if day == d0:
        return [(start_m, 24 * 60)]
    if day == d1:
        return [(0, end_m)]
    return [(0, 24 * 60)]  # middle day, full coverage


def _window_contains(rule: UserRule, day: date, minute: int) -> bool:
    """Does ``rule`` cover ``minute`` (since midnight) on ``day``?"""
    if rule.level == LEVEL_DEFAULT:
        start_m, end_m = _window_mins(rule)
        return start_m is not None and end_m is not None and start_m <= minute < end_m
    if rule.level == LEVEL_WEEKDAY:
        if day.weekday() not in rule.days:
            return False
        start_m, end_m = _window_mins(rule)
        return start_m is not None and end_m is not None and start_m <= minute < end_m
    # date level
    for seg_start, seg_end in _date_rule_segments(rule, day):
        if seg_start <= minute < seg_end:
            return True
    return False


def expand_day(rules: list[UserRule], day: date) -> list[SlotWinner]:
    """Return the winning rule decision for each of 96 slots on ``day``.

    Walks the ladder (date > weekday > default) per slot; the first rule
    whose window contains the slot wins.  No match -> optimizer passthrough
    (action "optimizer", rule_index None).
    """
    winners: list[SlotWinner] = []
    for slot in range(SLOTS_PER_DAY):
        minute = slot * SLOT_MINUTES
        winner: SlotWinner | None = None
        for level in LEVELS_STRONGEST_FIRST:
            for idx, rule in enumerate(rules):
                if rule.level != level:
                    continue
                if _window_contains(rule, day, minute):
                    if rule.action == ACTION_OPTIMIZER:
                        # optimizer action = passthrough; no rule owns the slot
                        winner = SlotWinner(
                            action=ACTION_OPTIMIZER,
                            pv_sell=rule.pv_sell,
                            level=level,
                        )
                    else:
                        winner = SlotWinner(
                            action=rule.action,
                            soc_target=rule.soc_target,
                            pv_sell=rule.pv_sell,
                            level=level,
                            rule_index=idx,
                        )
                    break
            if winner is not None:
                break
        if winner is None:
            winner = SlotWinner(action=ACTION_OPTIMIZER)
        winners.append(winner)
    return winners


def action_to_byte(action: str, soc_target: int | None) -> int:
    """Encode a manual action to an Emaldo slot byte."""
    if action == ACTION_IDLE:
        return SLOT_IDLE
    if action == ACTION_CHARGE:
        return min(max(int(soc_target or 100), 1), 100)
    if action == ACTION_DISCHARGE:
        target = min(max(int(soc_target or 0), 0), 100)
        return (256 - target) & 0xFF
    if action == ACTION_ORIGINAL:
        return SLOT_NO_OVERRIDE
    raise ValueError(f"Cannot encode action '{action}'")


def mask_plan(
    optimizer_bytes: list[int],
    optimizer_pv: list[bool],
    winners: list[SlotWinner],
) -> tuple[list[int], list[bool], list[str], list[str]]:
    """Apply user winners to the optimizer's byte/PV plan.

    Returns (masked_bytes, masked_pv, sources, pv_sources), each len 96.
    sources[i]: 'user' where a manual rule won, 'internal' where an
    'original' rule won, 'optimizer' otherwise.  pv_sources[i]: 'user'
    where the rule set PV explicitly, 'optimizer' where inherited.
    """
    masked_bytes: list[int] = []
    masked_pv: list[bool] = []
    sources: list[str] = []
    pv_sources: list[str] = []
    for i, winner in enumerate(winners):
        opt_byte = optimizer_bytes[i] if i < len(optimizer_bytes) else SLOT_NO_OVERRIDE
        opt_pv = optimizer_pv[i] if i < len(optimizer_pv) else True
        if winner.action == ACTION_OPTIMIZER:
            masked_bytes.append(opt_byte)
            sources.append("optimizer")
        elif winner.action == ACTION_ORIGINAL:
            masked_bytes.append(SLOT_NO_OVERRIDE)
            sources.append("internal")
        else:
            masked_bytes.append(action_to_byte(winner.action, winner.soc_target))
            sources.append("user")
        if winner.pv_sell == PV_INHERIT:
            masked_pv.append(opt_pv)
            pv_sources.append("optimizer")
        else:
            masked_pv.append(winner.pv_sell == PV_CHARGE)
            pv_sources.append("user")
    return masked_bytes, masked_pv, sources, pv_sources


def sources_summary(sources: list[str], masked_bytes: list[int]) -> str:
    """'4C 2D 0I' summary of user-owned slots only (Task 4 sensor)."""
    n_charge = n_discharge = n_idle = 0
    for i, src in enumerate(sources):
        if src != "user":
            continue
        byte = masked_bytes[i] if i < len(masked_bytes) else 0
        if byte == 0:
            n_idle += 1
        elif byte > 0x80:
            n_discharge += 1
        elif byte <= 100:
            n_charge += 1
        else:
            n_idle += 1
    return f"{n_charge}C {n_discharge}D {n_idle}I"
