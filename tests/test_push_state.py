"""Pure tests for push_state (issue #36): re-assert and read-back decisions."""
from datetime import datetime, timedelta, timezone

import tests

push_state = tests.load_coordinator()[0].push_state

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _decide(**kw):
    args = dict(
        now=NOW,
        last_push_at=None,
        last_push_ok_at=None,
        last_push_fail_at=None,
        fail_streak=0,
        accuracy=None,
    )
    args.update(kw)
    return push_state.decide_reassert(**args)


# ── decide_reassert ───────────────────────────────────────────────────


def test_never_pushed_reasserts():
    assert _decide() == (True, "no_successful_push")


def test_fresh_unjudged_push_is_left_alone():
    assert _decide(last_push_at=NOW - timedelta(seconds=60)) == (False, None)


def test_recent_failure_reasserts():
    ok, why = _decide(
        last_push_ok_at=NOW - timedelta(minutes=5),
        last_push_fail_at=NOW - timedelta(minutes=2),
        fail_streak=1,
    )
    assert (ok, why) == (True, "push_failed_recently")


def test_old_failure_does_not_reassert_on_its_own():
    assert _decide(
        last_push_ok_at=NOW - timedelta(minutes=5),
        last_push_fail_at=NOW - timedelta(minutes=20),
        fail_streak=1,
    ) == (False, None)


def test_recent_ok_push_is_not_reasserted():
    assert _decide(last_push_ok_at=NOW - timedelta(minutes=10)) == (False, None)


def test_stale_ok_push_reasserts():
    assert _decide(last_push_ok_at=NOW - timedelta(minutes=40)) == (True, "stale_push")


def test_accuracy_shortfall_reasserts_after_cooldown():
    acc = {"elapsed_slots": 3, "planned_discharge_kwh": 0.84, "actual_discharge_kwh": 0.02}
    assert _decide(last_push_ok_at=NOW - timedelta(minutes=20), accuracy=acc) == (
        True, "accuracy_divergence",
    )


def test_accuracy_shortfall_waits_for_cooldown():
    acc = {"elapsed_slots": 3, "planned_discharge_kwh": 0.84, "actual_discharge_kwh": 0.02}
    assert _decide(last_push_ok_at=NOW - timedelta(minutes=5), accuracy=acc) == (False, None)


def test_naive_and_aware_timestamps_compare():
    naive_ok = (NOW - timedelta(minutes=40)).replace(tzinfo=None)
    assert _decide(last_push_ok_at=naive_ok) == (True, "stale_push")


# ── current_slot_source ───────────────────────────────────────────────


def _chart(source):
    return [
        {"t": "2026-11-02T11:45:00+00:00", "price": 5.0, "source": "override"},
        {"t": "2026-11-02T12:00:00+00:00", "price": 5.0, "source": source},
        {"t": "2026-11-02T12:15:00+00:00", "price": 5.0, "source": "override"},
    ]


def test_slot_source_picks_the_slot_containing_now():
    assert push_state.current_slot_source(_chart("internal"), NOW) == "internal"
    assert push_state.current_slot_source(
        _chart("internal"), NOW + timedelta(minutes=14)
    ) == "internal"
    assert push_state.current_slot_source(
        _chart("internal"), NOW + timedelta(minutes=15)
    ) == "override"


def test_slot_source_normalises_case_and_whitespace():
    assert push_state.current_slot_source(_chart("  Override "), NOW) == "override"


def test_slot_source_is_none_without_a_usable_source():
    assert push_state.current_slot_source(_chart(None), NOW) is None
    assert push_state.current_slot_source(_chart(""), NOW) is None
    assert push_state.current_slot_source(None, NOW) is None
    assert push_state.current_slot_source("junk", NOW) is None
    assert push_state.current_slot_source([{"price": 1}], NOW) is None
    assert push_state.current_slot_source([{"t": "nonsense"}], NOW) is None


def test_slot_source_tolerates_naive_chart_times():
    chart = [{"t": "2026-11-02T12:00:00", "source": "internal"}]
    assert push_state.current_slot_source(chart, NOW) == "internal"
    assert push_state.current_slot_source(chart, NOW.replace(tzinfo=None)) == "internal"


# ── decide_enforcement ────────────────────────────────────────────────


def test_no_override_sent_means_nothing_to_enforce():
    assert push_state.decide_enforcement(sent_value=None, source="internal") == "none_sent"
    assert push_state.decide_enforcement(sent_value=0x80, source="internal") == "none_sent"


def test_override_read_back_is_confirmed():
    assert push_state.decide_enforcement(sent_value=0x00, source="override") == "confirmed"
    assert push_state.decide_enforcement(sent_value=0xF8, source="e2e_override") == "confirmed"


def test_internal_read_back_is_lost():
    assert push_state.decide_enforcement(sent_value=0xF8, source="internal") == "lost"
    assert push_state.decide_enforcement(sent_value=0x64, source="ai") == "lost"


def test_unknown_wording_is_unverifiable_not_lost():
    # A ha-emaldo wording change must degrade to "cannot tell", never to a
    # false "lost" that would re-push every tick.
    assert push_state.decide_enforcement(sent_value=0xF8, source=None) == "unverifiable"
    assert push_state.decide_enforcement(sent_value=0xF8, source="whatever") == "unverifiable"
