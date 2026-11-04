"""Config-flow wiring for the optional publish hour (issue #29).

``config_flow.py`` imports homeassistant at module load, and neither
voluptuous nor probatio is installable locally, so the config flow cannot be
imported in a local test.  These tests therefore assert the WIRING on the
parsed AST of config_flow.py - the same approach
test_grid_charge_solar_aware_coord.py uses for the optimize() call site.

The blank-detection DECISION is tested behaviourally in
tests/test_optional_value.py, against the same pure module the config flow
imports.
"""

from __future__ import annotations

import ast
from pathlib import Path

from . import load_optional_value

optional_value, const = load_optional_value()
is_blank = optional_value.is_blank

_CONFIG_FLOW_SRC = (
    Path(__file__).resolve().parents[1]
    / "custom_components" / "battery_optimizer" / "config_flow.py"
)

CONF_PUBLISH_HOUR = "CONF_PRECHARGE_PUBLISH_HOUR"


def _tree() -> ast.Module:
    return ast.parse(_CONFIG_FLOW_SRC.read_text(encoding="utf-8"))


def _publish_hour_schema_value() -> ast.expr:
    """The value expression bound to CONF_PRECHARGE_PUBLISH_HOUR in the schema."""
    for node in ast.walk(_tree()):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Call)
                and isinstance(key.func, ast.Attribute)
                and key.func.attr == "Optional"
                and key.args
                and isinstance(key.args[0], ast.Name)
                and key.args[0].id == CONF_PUBLISH_HOUR
            ):
                return value
    raise AssertionError(f"no {CONF_PUBLISH_HOUR} entry found in the schema")


def test_publish_hour_uses_the_tolerant_selector():
    """The schema entry must be the tolerant selector, not a bare NumberSelector."""
    value = _publish_hour_schema_value()
    assert isinstance(value, ast.Call), (
        f"publish hour schema value is {type(value).__name__}, expected a call"
    )
    func = value.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    assert name == "_OptionalNumberSelector", (
        f"publish hour uses {name!r}, expected '_OptionalNumberSelector' - a bare "
        "NumberSelector rejects a blank with 'expected float'"
    )


def test_tolerant_selector_subclasses_number_selector():
    """It must remain a NumberSelector so the frontend still gets a number input."""
    classes = [
        n
        for n in ast.walk(_tree())
        if isinstance(n, ast.ClassDef) and n.name == "_OptionalNumberSelector"
    ]
    assert classes, "_OptionalNumberSelector class not found"
    bases = [b.id for b in classes[0].bases if isinstance(b, ast.Name)]
    assert "NumberSelector" in bases, (
        f"_OptionalNumberSelector bases are {bases}, expected NumberSelector - "
        "the widget depends on isinstance(schema, selector.Selector)"
    )


def test_tolerant_selector_call_delegates_blank_to_is_blank():
    """__call__ must consult is_blank and return None for a blank."""
    classes = [
        n
        for n in ast.walk(_tree())
        if isinstance(n, ast.ClassDef) and n.name == "_OptionalNumberSelector"
    ]
    assert classes, "_OptionalNumberSelector class not found"
    calls = [
        n
        for n in classes[0].body
        if isinstance(n, ast.FunctionDef) and n.name == "__call__"
    ]
    assert calls, "_OptionalNumberSelector.__call__ not found"
    body = ast.dump(calls[0])
    assert "is_blank" in body, "__call__ does not consult is_blank"
    assert "super" in body, "__call__ does not delegate the non-blank case"


def test_options_flow_strips_a_blank_before_saving():
    """The blank must be removed from user_input before async_create_entry."""
    tree = _tree()
    flows = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "BatteryOptimizerOptionsFlow"
    ]
    assert flows, "BatteryOptimizerOptionsFlow class not found"
    steps = [
        n
        for n in flows[0].body
        # ast.AsyncFunctionDef is a SIBLING of ast.FunctionDef (it is not a
        # subclass), and async_step_init is an ``async def`` - matching only
        # ast.FunctionDef would never find it.
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "async_step_init"
    ]
    assert steps, "async_step_init not found"
    pop_line = None
    create_line = None
    for node in ast.walk(steps[0]):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "pop"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == CONF_PUBLISH_HOUR
        ):
            pop_line = node.lineno
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "async_create_entry"
        ):
            create_line = node.lineno
    assert pop_line is not None, "publish hour is never popped from user_input"
    assert create_line is not None, "async_create_entry call not found"
    assert pop_line < create_line, (
        f"the publish hour is popped at line {pop_line} but the entry is created "
        f"at line {create_line} - the blank would be stored"
    )


def test_blank_detection_is_the_pure_helper_the_config_flow_imports():
    """The config flow must import the same is_blank the tests exercise."""
    tree = _tree()
    imported = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "optional_value":
            if any(a.name == "is_blank" for a in node.names):
                assert node.level == 1, (
                    "the optional_value import must be relative (level 1) - an "
                    "absolute import would fail at runtime"
                )
                imported = True
    assert imported, "config_flow does not import is_blank from .optional_value"
    # And the helper really does treat a blank as blank.
    assert is_blank(None) is True
    assert is_blank("") is True
    assert is_blank(0) is False


def test_strip_is_guarded_by_is_blank():
    """The strip must be conditional.

    An unconditional user_input.pop() would pass every other test and silently
    discard a legitimate publish hour of 0 (midnight) - exactly the regression
    is_blank exists to prevent.
    """
    tree = _tree()
    flows = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "BatteryOptimizerOptionsFlow"
    ]
    assert flows, "BatteryOptimizerOptionsFlow class not found"
    steps = [
        n
        for n in flows[0].body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "async_step_init"
    ]
    assert steps, "async_step_init not found"
    guarded = None
    for node in ast.walk(steps[0]):
        if isinstance(node, ast.If) and "is_blank" in ast.dump(node.test):
            guarded = node
            break
    assert guarded is not None, (
        "the publish-hour strip is not guarded by is_blank - an unconditional pop "
        "would silently discard a legitimate publish hour of 0 (midnight)"
    )


def test_selector_type_is_not_overridden():
    """selector_type is a class attribute; overriding it would change the widget.

    HA's Selector.serialize() reads self.selector_type, so a subclass that
    reassigned it would render a different widget while every other assertion
    still passed.
    """
    classes = [
        n
        for n in ast.walk(_tree())
        if isinstance(n, ast.ClassDef) and n.name == "_OptionalNumberSelector"
    ]
    assert classes, "_OptionalNumberSelector class not found"
    assigned = {
        target.id
        for node in classes[0].body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "selector_type" not in assigned, (
        "_OptionalNumberSelector overrides selector_type - HA's Selector.serialize() "
        "reads self.selector_type, so the widget would change"
    )


def test_is_blank_is_evaluated_before_super():
    """The blank branch must come first, or a blank would be coerced and raise."""
    classes = [
        n
        for n in ast.walk(_tree())
        if isinstance(n, ast.ClassDef) and n.name == "_OptionalNumberSelector"
    ]
    assert classes, "_OptionalNumberSelector class not found"
    calls = [
        n
        for n in classes[0].body
        if isinstance(n, ast.FunctionDef) and n.name == "__call__"
    ]
    assert calls, "_OptionalNumberSelector.__call__ not found"
    guard_line = None
    super_line = None
    for node in calls[0].body:
        if isinstance(node, ast.If) and "is_blank" in ast.dump(node.test):
            guard_line = node.lineno
        if isinstance(node, ast.Return) and "super" in ast.dump(node.value):
            super_line = node.lineno
    assert guard_line is not None, "__call__ has no is_blank guard"
    assert super_line is not None, "__call__ has no super() delegation"
    assert guard_line < super_line, (
        f"the super() delegation at line {super_line} comes before the is_blank "
        f"guard at line {guard_line} - a blank would be coerced and raise"
    )


def test_optional_value_import_is_relative():
    """No import of optional_value may be absolute.

    ``from optional_value import is_blank`` resolves against the top-level
    namespace, not the integration package, so the component fails to load.
    """
    imports = [
        node
        for node in ast.walk(_tree())
        if isinstance(node, ast.ImportFrom) and node.module == "optional_value"
    ]
    assert imports, "config_flow does not import optional_value at all"
    assert all(node.level == 1 for node in imports), (
        "config_flow imports optional_value absolutely - it must be "
        ".optional_value (level 1) or the component fails to load"
    )
