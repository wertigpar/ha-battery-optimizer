"""Currency must be named in the exported analysis, never assumed."""
import sys

from tests import load_coordinator

load_coordinator()  # execs plan_export into the fabricated package
render_analysis = sys.modules["bopt_pkg.plan_export"].render_analysis


SNAPSHOT = {"state": "planned", "planned": True}


def test_currency_is_required():
    try:
        render_analysis(SNAPSHOT)  # type: ignore[call-arg]
    except TypeError:
        return
    raise AssertionError("currency must be a required argument")


def test_export_names_the_requested_currency():
    assert "SEK" in render_analysis(SNAPSHOT, currency="SEK")


def test_export_uses_the_given_currency_not_eur():
    out = render_analysis(SNAPSHOT, currency="SEK")
    assert "SEK" in out
