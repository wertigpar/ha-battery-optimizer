"""Restart-marker file I/O.

Pure-module tests (marker_io.py): no HA imports, no relative imports.

The three-state read is load-bearing: "no file" and "file I cannot read" are
different situations.  A corrupt marker is not "nothing to restore" -- treating
it as such switches a safety net off for the rest of the session.
"""

from __future__ import annotations

import json
import os

from . import load_marker_io

marker_io, const = load_marker_io()
MARKER_UNREADABLE = marker_io.MARKER_UNREADABLE


def _tmp_path(tmp_path, name="marker.json"):
    return str(tmp_path / name)


def test_write_then_read_round_trips(tmp_path):
    path = _tmp_path(tmp_path)
    payload = {"we_enabled": True, "at": "2026-10-20T18:19:54+03:00"}
    marker_io.atomic_write_json(path, payload)
    assert marker_io.read_json_marker(path) == payload


def test_write_leaves_no_temp_file(tmp_path):
    path = _tmp_path(tmp_path)
    marker_io.atomic_write_json(path, {"a": 1})
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "marker.json"]
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_write_replaces_an_existing_file(tmp_path):
    path = _tmp_path(tmp_path)
    marker_io.atomic_write_json(path, {"a": 1})
    marker_io.atomic_write_json(path, {"a": 2})
    assert marker_io.read_json_marker(path) == {"a": 2}


def test_missing_file_reads_as_none(tmp_path):
    assert marker_io.read_json_marker(_tmp_path(tmp_path)) is None


def test_invalid_json_reads_as_unreadable(tmp_path):
    path = _tmp_path(tmp_path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert marker_io.read_json_marker(path) is MARKER_UNREADABLE


def test_json_that_is_not_an_object_reads_as_unreadable(tmp_path):
    path = _tmp_path(tmp_path)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([1, 2, 3], fh)
    assert marker_io.read_json_marker(path) is MARKER_UNREADABLE


def test_unreadable_is_distinct_from_none(tmp_path):
    path = _tmp_path(tmp_path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("garbage")
    assert marker_io.read_json_marker(path) is not None
    assert marker_io.read_json_marker(path) is MARKER_UNREADABLE


def test_write_raises_oserror_for_an_unwritable_path(tmp_path):
    bad = str(tmp_path / "no_such_dir" / "marker.json")
    try:
        marker_io.atomic_write_json(bad, {"a": 1})
    except OSError:
        return
    raise AssertionError("expected OSError for an unwritable path")


def test_is_unreadable_recognises_the_sentinel(tmp_path):
    path = _tmp_path(tmp_path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("garbage")
    value = marker_io.read_json_marker(path)
    assert marker_io.is_unreadable(value) is True


def test_is_unreadable_is_false_for_everything_else(tmp_path):
    path = _tmp_path(tmp_path)
    assert marker_io.is_unreadable(None) is False
    assert marker_io.is_unreadable({}) is False
    assert marker_io.is_unreadable(marker_io.atomic_write_json) is False
    marker_io.atomic_write_json(path, {"a": 1})
    assert marker_io.is_unreadable(marker_io.read_json_marker(path)) is False
