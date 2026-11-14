"""Issue #42: the reserved-capacity fields are wired into the flow schema."""
import ast
from pathlib import Path

SRC = (
    Path(__file__).resolve().parents[1]
    / "custom_components" / "battery_optimizer" / "config_flow.py"
)

NEW_KEYS = (
    "CONF_BATTERY_RANGE_LOW_MARKER",
    "CONF_RESERVED_BOTTOM_KWH",
    "CONF_RESERVED_TOP_KWH",
)


def _tree():
    return ast.parse(SRC.read_text(encoding="utf-8"))


def test_both_flow_steps_validate_reserved_capacity():
    found = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in (
            "async_step_user",
            "async_step_init",
        ):
            called = {
                n.func.id
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            if "_reserved_error_fields" in called:
                found.add(node.name)
    assert found == {"async_step_user", "async_step_init"}


def test_new_fields_are_optional():
    optional = set()
    for node in ast.walk(_tree()):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Optional"
            and node.args
        ):
            key = node.args[0]
            # The const names are imported bare, so ``vol.Optional(CONF_X)``
            # holds an ast.Name; dotted forms hold an ast.Attribute.
            if isinstance(key, ast.Name):
                optional.add(key.id)
            elif isinstance(key, ast.Attribute):
                optional.add(key.attr)
    assert set(NEW_KEYS) <= optional


def test_options_form_shows_the_effective_band():
    tree = _tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_step_init":
            texts = {
                k.value
                for d in ast.walk(node)
                if isinstance(d, ast.Dict)
                for k in d.keys
                if isinstance(k, ast.Constant)
            }
            assert "effective_band" in texts
            return
    raise AssertionError("async_step_init not found")
