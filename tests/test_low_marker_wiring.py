"""Issue #42: every push path resolves the Battery Range low marker the same way.

A source-level check, because the three sites are async coordinator methods
that need a live Home Assistant install to exercise end to end.  It fails if
any site drifts back to a raw planned floor.
"""
import ast
from pathlib import Path

_SRC = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "battery_optimizer"
    / "coordinator.py"
)

_PUSH_METHODS = ("_push_schedule", "_push_guard_update", "_normalise_discharge_bytes")


def _class_methods():
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "BatteryOptimizerCoordinator":
            return {
                item.name: item
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    raise AssertionError("BatteryOptimizerCoordinator not found")


def _calls_resolved_low_marker(method):
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_resolved_low_marker"
        for node in ast.walk(method)
    )


def _raw_floor_marker_assignments(method):
    """int(...soc_min) calls used as a marker value, not the guard floor."""
    hits = []
    for node in ast.walk(method):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "int"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Attribute) and arg.attr == "soc_min":
                hits.append(ast.unparse(node))
    return hits


def test_all_three_push_paths_use_the_shared_resolver():
    methods = _class_methods()
    assert "_resolved_low_marker" in methods
    missing = [name for name in _PUSH_METHODS if not _calls_resolved_low_marker(methods[name])]
    assert missing == [], f"push paths bypass _resolved_low_marker: {missing}"


def test_no_push_path_sends_the_raw_planned_floor():
    methods = _class_methods()
    leaked = {
        name: hits
        for name in _PUSH_METHODS
        if (hits := _raw_floor_marker_assignments(methods[name]))
    }
    assert leaked == {}, f"raw int(...soc_min) marker still sent: {leaked}"
