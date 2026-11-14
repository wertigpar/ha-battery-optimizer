"""Issue #42: every translation file must carry the new GGR keys."""
import json
from pathlib import Path

BASE = (
    Path(__file__).resolve().parents[1]
    / "custom_components" / "battery_optimizer"
)

FIELDS = ["battery_range_low_marker", "reserved_bottom_kwh", "reserved_top_kwh"]
ERRORS = ["invalid_reserved_band", "reserved_exceeds_capacity"]
FILES = ["strings.json"] + [
    f"translations/{lang}.json" for lang in ("en", "fi", "sv", "da", "nb")
]


def _load(name):
    return json.loads((BASE / name).read_text(encoding="utf-8"))


def test_every_file_has_the_new_field_labels():
    for name in FILES:
        doc = _load(name)
        steps = {
            "config.step.user": doc["config"]["step"]["user"],
            "options.step.init": doc["options"]["step"]["init"],
        }
        for where, step in steps.items():
            data = step["data"]
            for field in FIELDS:
                assert field in data, f"{name}: missing {where}.data.{field}"
                assert data[f"{field}_description"].strip(), (
                    f"{name}: empty {where}.data.{field}_description"
                )


def test_every_file_has_the_new_error_keys():
    for name in FILES:
        doc = _load(name)
        for block in (doc["config"]["error"], doc["options"]["error"]):
            for key in ERRORS:
                assert key in block, f"{name}: missing error key {key}"
