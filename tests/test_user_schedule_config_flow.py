"""User schedule config subentry flow tests."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant

from custom_components.battery_optimizer.config_flow import (
    RuleSubentryFlow,
)
from custom_components.battery_optimizer.const import DEFAULT_RULE_LABEL, DOMAIN

# These tests exercise the flow via a minimal mock config entry because
# the repo's test setup is not a full pytest-homeassistant-custom-component
# environment.  Core-integration-level coverage is out of scope here; the
# flow's validation logic is covered directly.


def make_entry() -> ConfigEntry:
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "entry_1"
    entry.domain = DOMAIN
    entry.subentries = {}
    return entry


def make_hass() -> HomeAssistant:
    hass = MagicMock(spec=HomeAssistant)
    return hass


def test_device_subentry_id():
    """coordinator._device_subentry_id resolves the device container subentry."""
    from custom_components.battery_optimizer.const import SUBENTRY_TYPE_DEVICE
    from custom_components.battery_optimizer.coordinator import (
        BatteryOptimizerCoordinator,
    )

    coord = object.__new__(BatteryOptimizerCoordinator)
    entry = MagicMock()
    coord._entry = entry

    # no subentries at all — .values() returns empty list
    entry.subentries.values.return_value = []
    assert coord._device_subentry_id() is None

    # only schedule-rule subentries
    rule_sub = MagicMock()
    rule_sub.subentry_type = "rule"
    rule_sub.subentry_id = "sub_rule"
    entry.subentries.values.return_value = [rule_sub]
    assert coord._device_subentry_id() is None

    # a device container subentry present
    device_sub = MagicMock()
    device_sub.subentry_type = SUBENTRY_TYPE_DEVICE
    device_sub.subentry_id = "sub_device"
    entry.subentries.values.return_value = [rule_sub, device_sub]
    assert coord._device_subentry_id() == "sub_device"


def test_flow_registers_rule_subentry_type():
    from custom_components.battery_optimizer.config_flow import (
        BatteryOptimizerConfigFlow,
    )

    types = BatteryOptimizerConfigFlow.async_get_supported_subentry_types(
        make_entry()
    )
    assert "rule" in types
    assert issubclass(types["rule"], RuleSubentryFlow)


def test_flow_create_returns_abort_on_overlap():
    """Overlap with an existing same-level rule blocks creation."""
    flow = RuleSubentryFlow()
    entry = make_entry()
    # an existing Monday 07:00-17:00 rule
    entry.subentries = {
        "s1": ConfigSubentry(
            subentry_id="s1",
            subentry_type="rule",
            title="Existing",
            unique_id="s1",
            data={
                "level": "weekday", "days": [0], "start_date": None,
                "end_date": None, "start_time": "07:00", "end_time": "17:00",
                "action": "idle", "soc_target": None, "pv_sell": "inherit",
                "label": "Existing",
            },
        )
    }
    flow.hass = make_hass()
    flow.handler = (entry.entry_id, "rule")
    flow.context = {"source": "user"}
    flow._get_entry = MagicMock(return_value=entry)

    # create flow is level-menu-first: step_user(None) shows the level menu
    menu = asyncio.run(flow.async_step_user(None))
    assert menu["type"] == "menu"
    assert set(menu["menu_options"]) == {"weekday", "date", "default"}

    # picking a level shows the action menu; picking an action routes to
    # the detail form.  Neither level nor action is a form field — both
    # come from the menu routes.
    action_menu = asyncio.run(flow.async_step_weekday(None))
    assert action_menu["type"] == "menu"
    assert set(action_menu["menu_options"]) == {
        "charge", "idle", "discharge", "original", "optimizer",
    }
    form = asyncio.run(flow.async_step_idle(None))
    assert form["type"] == "form"
    assert form["step_id"] == "detail_idle"

    user_input = {
        "days": [0], "start_date": None, "end_date": None,
        "start_time": "12:00", "end_time": "20:00",
        "soc_target": None, "pv_sell": "inherit", "label": "New",
    }
    result = asyncio.run(flow.async_step_idle(user_input))
    assert result["type"] == "form"
    assert "overlap" in result.get("errors", {}).get("base", "").lower()


def _make_rule(
    subentry_id: str,
    *,
    level: str = "weekday",
    days: list[int] | None = None,
    start_time: str,
    end_time: str,
    action: str = "idle",
    label: str = "",
    soc_target: int | None = None,
) -> ConfigSubentry:
    """Build a minimal weekday rule subentry."""
    return ConfigSubentry(
        subentry_id=subentry_id,
        subentry_type="rule",
        title=label,
        unique_id=subentry_id,
        data={
            "level": level,
            "days": days if days is not None else [0],
            "start_date": None,
            "end_date": None,
            "start_time": start_time,
            "end_time": end_time,
            "action": action,
            "soc_target": soc_target,
            "pv_sell": "inherit",
            "label": label,
        },
    )


def _make_reconfigure_flow(entry: ConfigEntry, subentry_id: str) -> RuleSubentryFlow:
    """Reconfigure flow whose subentry lookup resolves against ``entry``."""
    flow = RuleSubentryFlow()
    flow.hass = make_hass()
    flow.handler = (entry.entry_id, "rule")
    flow.context = {"source": "reconfigure", "subentry_id": subentry_id}
    # _get_reconfigure_subentry reads hass.config_entries.async_get_known_entry
    # directly; _get_entry is used by _process/_same_level_siblings.
    flow.hass.config_entries.async_get_known_entry.return_value = entry
    flow._get_entry = MagicMock(return_value=entry)
    return flow


def test_flow_create_success():
    """Valid non-overlapping input creates the rule subentry."""
    flow = RuleSubentryFlow()
    entry = make_entry()
    # adjacent existing rule (Mon 07:00-17:00) — new window touches but
    # does not overlap it (end-exclusive windows)
    entry.subentries = {
        "s1": _make_rule("s1", start_time="07:00", end_time="17:00", label="Existing")
    }
    flow.hass = make_hass()
    flow.handler = (entry.entry_id, "rule")
    flow.context = {"source": "user"}
    flow._get_entry = MagicMock(return_value=entry)

    # level menu -> action menu -> detail form (level comes from the route)
    menu = asyncio.run(flow.async_step_user(None))
    assert menu["type"] == "menu"
    assert set(menu["menu_options"]) == {"weekday", "date", "default"}
    action_menu = asyncio.run(flow.async_step_weekday(None))
    assert action_menu["type"] == "menu"
    form = asyncio.run(flow.async_step_charge(None))
    assert form["type"] == "form"

    user_input = {
        "days": [0], "start_date": None, "end_date": None,
        "start_time": "17:00", "end_time": "19:00",
        "soc_target": 90, "pv_sell": "inherit", "label": "New",
    }
    result = asyncio.run(flow.async_step_charge(user_input))
    assert result["type"] == "create_entry"
    assert result["data"]["level"] == "weekday"
    # action is merged from the menu route (not in the submitted form data)
    assert result["data"]["action"] == "charge"
    assert result["data"]["soc_target"] == 90
    assert set(result["data"].keys()) == {
        "level", "days", "start_date", "end_date", "start_time", "end_time",
        "action", "soc_target", "pv_sell", "label",
    }


def test_framework_roundtrip_routes_menu_to_detail_steps():
    """The full framework path: level menu -> action menu -> detail form
    -> submit routed through the ``detail_<action>`` forwarding handler.

    Mirrors data_entry_flow._async_handle_step: a menu pick calls
    ``async_step_<next_step_id>(None)`` — level keys route to the level
    steps (weekday/date/default), action keys to the action steps — and a
    form submit routes back to ``async_step_<step_id>``, which is
    ``detail_<action>``.  The level is injected from the first menu and
    must not appear in the form.
    """
    flow = RuleSubentryFlow()
    entry = make_entry()
    entry.subentries = {}
    flow.hass = make_hass()
    flow.handler = (entry.entry_id, "rule")
    flow.context = {"source": "user"}
    flow._get_entry = MagicMock(return_value=entry)

    # level menu -> framework calls async_step_weekday(None) -> action menu
    # -> framework calls async_step_charge(None) -> detail form
    menu = asyncio.run(flow.async_step_user(None))
    assert menu["type"] == "menu"
    assert set(menu["menu_options"]) == {"weekday", "date", "default"}
    # every level-menu key must be a real step method (the framework
    # dispatches to it without further checking)
    for key in menu["menu_options"]:
        assert hasattr(flow, f"async_step_{key}")
    action_menu = asyncio.run(flow.async_step_weekday(None))
    assert action_menu["type"] == "menu"
    assert set(action_menu["menu_options"]) == {
        "charge", "idle", "discharge", "original", "optimizer",
    }
    for key in action_menu["menu_options"]:
        assert hasattr(flow, f"async_step_{key}")
    assert action_menu["step_id"] == "action"
    assert hasattr(flow, "async_step_action")  # menu step_id must exist
    form = asyncio.run(flow.async_step_charge(None))
    assert form["type"] == "form"
    assert form["step_id"] == "detail_charge"
    assert "soc_target" in {
        key.schema for key in form["data_schema"].schema
    }
    assert "level" not in {
        key.schema for key in form["data_schema"].schema
    }

    # form submit -> framework calls async_step_detail_charge(user_input)
    user_input = {
        "days": [0], "start_date": None, "end_date": None,
        "start_time": "17:00", "end_time": "19:00",
        "soc_target": 100, "pv_sell": "inherit", "label": "New",
    }
    result = asyncio.run(flow.async_step_detail_charge(user_input))
    assert result["type"] == "create_entry"
    assert result["data"]["action"] == "charge"
    assert result["data"]["level"] == "weekday"
    assert result["data"]["soc_target"] == 100


def test_framework_roundtrip_reconfigure_updates_not_creates():
    """Reconfigure roundtrip: level menu -> action menu -> detail form ->
    submit via the ``detail_<action>`` forwarding step commits an UPDATE,
    not a create.

    The framework never passes a reconfigure flag to step methods — it
    calls ``async_step_<menu_key>(None)`` / ``async_step_<step_id>(...)``
    with one positional arg.  The flow must detect the edit mode from
    ``self.source`` (SOURCE_RECONFIGURE) and route to
    ``async_update_and_abort``.
    """
    entry = make_entry()
    entry.subentries = {
        "s1": _make_rule("s1", start_time="07:00", end_time="17:00", label="Existing")
    }
    flow = _make_reconfigure_flow(entry, "s1")

    # reconfigure flow: level menu -> weekday -> action menu -> idle
    menu = asyncio.run(flow.async_step_reconfigure(None))
    assert menu["type"] == "menu"
    assert set(menu["menu_options"]) == {"weekday", "date", "default"}
    action_menu = asyncio.run(
        flow.async_step_weekday(None)
    )
    assert action_menu["type"] == "menu"
    form = asyncio.run(flow.async_step_idle(None))
    assert form["type"] == "form"
    assert form["step_id"] == "detail_idle"

    user_input = {
        "days": [0], "start_date": None, "end_date": None,
        "start_time": "07:00", "end_time": "20:00",
        "soc_target": None, "pv_sell": "inherit", "label": "Existing",
    }
    # form submit -> framework calls async_step_detail_idle(user_input)
    result = asyncio.run(flow.async_step_detail_idle(user_input))
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    call = flow.hass.config_entries.async_update_subentry.call_args
    assert call.kwargs["data"]["end_time"] == "20:00"


def test_flow_idle_detail_has_no_soc_target():
    """Idle rules never carry a SoC target — the detail form omits the
    field entirely and the stored rule gets soc_target None."""
    flow = RuleSubentryFlow()
    entry = make_entry()
    entry.subentries = {}
    flow.hass = make_hass()
    flow.handler = (entry.entry_id, "rule")
    flow.context = {"source": "user"}
    flow._get_entry = MagicMock(return_value=entry)

    menu = asyncio.run(flow.async_step_user(None))
    assert menu["type"] == "menu"
    assert set(menu["menu_options"]) == {"weekday", "date", "default"}
    action_menu = asyncio.run(flow.async_step_weekday(None))
    assert action_menu["type"] == "menu"
    # the idle detail form exposes no soc_target field
    form = asyncio.run(flow.async_step_idle(None))
    assert form["type"] == "form"
    assert "soc_target" not in {
        key.schema for key in form["data_schema"].schema
    }

    # submit without soc_target -> stored rule has soc_target None
    user_input = {
        "days": [0], "start_date": None, "end_date": None,
        "start_time": "17:00", "end_time": "19:00",
        "pv_sell": "inherit", "label": "Idle",
    }
    result = asyncio.run(flow.async_step_idle(user_input))
    assert result["type"] == "create_entry"
    assert result["data"]["action"] == "idle"
    assert result["data"]["soc_target"] is None


def test_reconfigure_shows_existing_values():
    """Reconfigure form is pre-populated from the subentry's current data."""
    entry = make_entry()
    entry.subentries = {
        "s1": _make_rule(
            "s1",
            start_time="06:30",
            end_time="17:00",
            action="discharge",
            soc_target=50,
            label="Existing",
        )
    }
    flow = _make_reconfigure_flow(entry, "s1")

    # reconfigure flow is level-menu-first
    menu = asyncio.run(flow.async_step_reconfigure(None))
    assert menu["type"] == "menu"
    assert set(menu["menu_options"]) == {"weekday", "date", "default"}
    action_menu = asyncio.run(
        flow.async_step_weekday(None)
    )
    assert action_menu["type"] == "menu"

    # pick discharge -> detail form pre-populated from the subentry
    result = asyncio.run(flow.async_step_discharge(None))
    assert result["type"] == "form"
    assert result["errors"] == {}
    schema = result["data_schema"].schema
    defaults = {key.schema: key.default() for key in schema if key.default is not None}
    # non-schema-default values prove the form reads from the subentry
    assert defaults["start_time"] == "06:30"
    assert defaults["end_time"] == "17:00"
    assert defaults["soc_target"] == 50


def test_reconfigure_success():
    """Editing commits via async_update_and_abort with the new data."""
    entry = make_entry()
    entry.subentries = {
        "s1": _make_rule("s1", start_time="07:00", end_time="17:00", label="Existing")
    }
    flow = _make_reconfigure_flow(entry, "s1")

    menu = asyncio.run(flow.async_step_reconfigure(None))
    assert menu["type"] == "menu"
    action_menu = asyncio.run(
        flow.async_step_weekday(None)
    )
    assert action_menu["type"] == "menu"
    form = asyncio.run(flow.async_step_idle(None))
    assert form["type"] == "form"

    user_input = {
        "days": [0], "start_date": None, "end_date": None,
        "start_time": "07:00", "end_time": "20:00",
        "soc_target": None, "pv_sell": "inherit", "label": "Existing",
    }
    result = asyncio.run(flow.async_step_idle(user_input))
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    call = flow.hass.config_entries.async_update_subentry.call_args
    assert call.kwargs["entry"] is entry
    assert call.kwargs["subentry"].subentry_id == "s1"
    assert call.kwargs["data"]["end_time"] == "20:00"
    assert call.kwargs["data"]["label"] == "Existing"
    assert call.kwargs["data"]["action"] == "idle"


def test_reconfigure_edit_excludes_self_from_overlap():
    """Editing a rule may overlap a sibling (rejected) but self-overlap is allowed."""
    entry = make_entry()
    # s1 Mon 07:00-17:00, s2 Mon 17:00-19:00 (adjacent, valid)
    entry.subentries = {
        "s1": _make_rule("s1", start_time="07:00", end_time="17:00", label="A"),
        "s2": _make_rule("s2", start_time="17:00", end_time="19:00", label="B"),
    }

    # Edit s1 to Mon 12:00-20:00 -> overlaps s2 -> rejected with overlap error
    flow = _make_reconfigure_flow(entry, "s1")
    menu = asyncio.run(flow.async_step_reconfigure(None))
    assert menu["type"] == "menu"
    action_menu = asyncio.run(
        flow.async_step_weekday(None)
    )
    assert action_menu["type"] == "menu"
    form = asyncio.run(flow.async_step_idle(None))
    assert form["type"] == "form"
    moving = {
        "days": [0], "start_date": None, "end_date": None,
        "start_time": "12:00", "end_time": "20:00",
        "soc_target": None, "pv_sell": "inherit", "label": "A",
    }
    result = asyncio.run(flow.async_step_idle(moving))
    assert result["type"] == "form"
    assert "overlap" in result.get("errors", {}).get("base", "").lower()

    # Edit s2 to its own current window (unchanged) -> self excluded, no
    # sibling overlap -> succeeds.  Without self-exclusion s2 would overlap
    # itself and be rejected.
    flow = _make_reconfigure_flow(entry, "s2")
    unchanged = {
        "days": [0], "start_date": None, "end_date": None,
        "start_time": "17:00", "end_time": "19:00",
        "soc_target": None, "pv_sell": "inherit", "label": "B",
    }
    result = asyncio.run(flow.async_step_idle(unchanged))
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"


def test_reconfigure_schema_roundtrips_stored_values():
    """Reconfigure schema roundtrips stored rule values (framework path).

    HA's FlowManager applies ``data_schema(user_input)`` on EVERY submit
    (data_entry_flow._async_handle_step), and the reconfigure form is
    pre-populated from the stored ``subentry.data``.  Stored weekdays are
    ints (0=Mon..6=Sun) but the form's SelectSelector works in strings,
    so the schema must (a) convert stored int day defaults to the
    selector's string values and (b) accept the frontend's string day
    values on submit.  The other stored values (``None`` dates,
    ``None`` soc_target, ``level='default'``) must still validate as-is.
    """
    from custom_components.battery_optimizer.config_flow import _rule_schema

    shapes = [
        # (a) weekday charge rule — soc_target set
        _make_rule(
            "c1", start_time="07:00", end_time="12:00",
            action="charge", soc_target=50, label="Charge",
        ),
        # (b) weekday idle rule — soc_target None
        _make_rule(
            "i1", start_time="13:00", end_time="17:00",
            action="idle", label="Idle",
        ),
        # (c) default rule — level default, days [], soc_target None
        ConfigSubentry(
            subentry_id="d1", subentry_type="rule", title="Default",
            unique_id="default_rule",
            data={
                "level": "default", "days": [], "start_date": None,
                "end_date": None, "start_time": "00:00", "end_time": "24:00",
                "action": "optimizer", "soc_target": None,
                "pv_sell": "inherit", "label": "Default",
            },
        ),
    ]

    for sub in shapes:
        stored = dict(sub.data)
        # action and level live on the menu routes, not in the form schema
        schema = _rule_schema(
            stored, action=stored["action"], level=stored["level"]
        )
        # stored int days become the selector's string defaults; only
        # weekday rules expose the days field at all
        form_defaults = {
            key.schema: key.default()
            for key in schema.schema
            if key.default is not None
        }
        if stored["level"] == "weekday":
            assert form_defaults["days"] == [str(x) for x in stored["days"]]
        else:
            assert "days" not in form_defaults
        # frontend submits the form values back -> days are strings; the
        # action/level keys come from the menu routes, and soc_target only
        # exists in the charge/discharge schemas
        form_values = dict(stored)
        if stored["level"] != "weekday":
            form_values.pop("days")
        else:
            form_values["days"] = form_defaults["days"]  # ints -> strings
        if stored["level"] != "date":
            form_values.pop("start_date")
            form_values.pop("end_date")
        form_values.pop("action")
        form_values.pop("level")
        if stored["action"] not in ("charge", "discharge"):
            form_values.pop("soc_target")
        validated = schema(form_values)  # must not raise vol.Invalid
        assert "level" not in validated
        if stored["action"] in ("charge", "discharge"):
            assert validated["soc_target"] == stored["soc_target"]
        # date fields exist only in the date-level schema
        if stored["level"] == "date":
            assert validated["start_date"] is None
            assert validated["end_date"] is None

    # Framework path: the reconfigure form's own data_schema must accept
    # the frontend-shaped values (string days) on the next submit.
    entry = make_entry()
    entry.subentries = {"i1": shapes[1]}
    flow = _make_reconfigure_flow(entry, "i1")
    menu = asyncio.run(flow.async_step_reconfigure(None))
    assert menu["type"] == "menu"
    result = asyncio.run(flow.async_step_idle(None))
    defaults = {
        key.schema: key.default()
        for key in result["data_schema"].schema
        if key.default is not None
    }
    stored = dict(entry.subentries["i1"].data)
    form_data = {
        k: v for k, v in stored.items()
        if k not in ("action", "soc_target", "level", "start_date", "end_date")
    }
    form_data["days"] = defaults["days"]
    result["data_schema"](form_data)  # must not raise


def test_reconfigure_menus_mark_current_level_and_action():
    """Reconfigure menus label the stored level and action '(current)'."""
    entry = make_entry()
    entry.subentries = {
        "s1": ConfigSubentry(
            subentry_id="s1",
            subentry_type="rule",
            title="Charge rule",
            unique_id="s1",
            data={
                "level": "weekday", "days": [0], "start_date": None,
                "end_date": None, "start_time": "07:00", "end_time": "17:00",
                "action": "charge", "soc_target": 90, "pv_sell": "inherit",
                "label": "",
            },
        )
    }
    flow = _make_reconfigure_flow(entry, "s1")

    # level menu marks the stored level
    menu = asyncio.run(flow.async_step_reconfigure(None))
    assert menu["type"] == "menu"
    assert menu["menu_options"]["weekday"].endswith("(current)")
    assert not menu["menu_options"]["date"].endswith("(current)")

    # picking the current level -> action menu marks the stored action
    action_menu = asyncio.run(
        flow.async_step_weekday(None)
    )
    assert action_menu["type"] == "menu"
    assert action_menu["menu_options"]["charge"].endswith("(current)")
    assert not action_menu["menu_options"]["idle"].endswith("(current)")


def test_create_menus_do_not_mark_current_level_or_action():
    """Create menus leave all level and action labels unmarked."""
    flow = RuleSubentryFlow()
    entry = make_entry()
    entry.subentries = {}
    flow.hass = make_hass()
    flow.handler = (entry.entry_id, "rule")
    flow.context = {"source": "user"}
    flow._get_entry = MagicMock(return_value=entry)

    menu = asyncio.run(flow.async_step_user(None))
    assert menu["type"] == "menu"
    for label in menu["menu_options"].values():
        assert not label.endswith("(current)")

    # the action menu is likewise unmarked
    action_menu = asyncio.run(flow.async_step_weekday(None))
    assert action_menu["type"] == "menu"
    for label in action_menu["menu_options"].values():
        assert not label.endswith("(current)")


def test_ensure_default_rule_creates_when_missing():
    """A default 'optimizer everywhere' rule is added when none exists."""
    from custom_components.battery_optimizer import _ensure_default_rule

    entry = make_entry()
    entry.subentries = {}
    hass = make_hass()
    added = {}
    hass.config_entries.async_add_subentry = lambda entry_, sub: added.setdefault(
        sub.unique_id, sub
    )

    asyncio.run(_ensure_default_rule(hass, entry))

    assert "default_rule" in added
    sub = added["default_rule"]
    assert sub.unique_id == "default_rule"
    assert sub.subentry_type == "rule"
    data = dict(sub.data)
    assert data["level"] == "default"
    assert data["action"] == "optimizer"
    assert data["label"] == DEFAULT_RULE_LABEL


def test_ensure_default_rule_no_duplicate_when_present():
    """An existing default-rule subentry is left untouched (no duplicate)."""
    from custom_components.battery_optimizer import _ensure_default_rule

    entry = make_entry()
    entry.subentries = {
        "s1": ConfigSubentry(
            subentry_id="s1",
            subentry_type="rule",
            title="Default Schedule (Optimizer)",
            unique_id="default_rule",
            data={
                "level": "default",
                "days": [],
                "start_date": None,
                "end_date": None,
                "start_time": "00:00",
                "end_time": "24:00",
                "action": "optimizer",
                "soc_target": None,
                "pv_sell": "inherit",
                "label": DEFAULT_RULE_LABEL,
            },
        )
    }
    hass = make_hass()

    asyncio.run(_ensure_default_rule(hass, entry))

    hass.config_entries.async_add_subentry.assert_not_called()
    hass.config_entries.async_update_subentry.assert_not_called()


def test_ensure_default_rule_migrates_old_title():
    """A pre-rename default-rule subentry titled 'Default' is retitled."""
    from custom_components.battery_optimizer import _ensure_default_rule

    entry = make_entry()
    entry.subentries = {
        "s1": ConfigSubentry(
            subentry_id="s1",
            subentry_type="rule",
            title="Default",
            unique_id="default_rule",
            data={
                "level": "default",
                "days": [],
                "start_date": None,
                "end_date": None,
                "start_time": "00:00",
                "end_time": "24:00",
                "action": "optimizer",
                "soc_target": None,
                "pv_sell": "inherit",
                "label": "Default",
            },
        )
    }
    hass = make_hass()
    updated = {}
    hass.config_entries.async_update_subentry = (
        lambda entry_, sub, *, title, data=None: updated.setdefault(
            sub.subentry_id, {"title": title, "label": data.get("label") if data else None}
        )
    )

    asyncio.run(_ensure_default_rule(hass, entry))

    hass.config_entries.async_add_subentry.assert_not_called()
    # fixture action=optimizer → title carries the governing source
    assert updated.get("s1", {}).get("title") == "Default Schedule (Optimizer)"
    assert updated.get("s1", {}).get("label") == DEFAULT_RULE_LABEL


def _device_mock(bound_subentries):
    """Build a DeviceEntry-like mock with a config_entries_subentries dict."""
    dev = MagicMock()
    dev.id = "device_1"
    dev.config_entries_subentries = bound_subentries
    return dev


def test_ensure_device_subentry_creates_and_binds():
    """A missing device subentry is created and the device bound to it.

    The device itself is created via ``async_get_or_create`` WITHOUT a
    ``config_subentry_id`` (passing one on an existing device would move
    it silently — deprecated, breaks 2027.8); the subentry binding is
    moved explicitly with ``async_update_device``.
    """
    from unittest.mock import patch

    import homeassistant.helpers.device_registry as dr

    from custom_components.battery_optimizer import _ensure_device_subentry
    from custom_components.battery_optimizer.const import (
        DEVICE_SUBENTRY_LABEL,
        DEVICE_SUBENTRY_UNIQUE_ID,
        SUBENTRY_TYPE_DEVICE,
    )

    entry = make_entry()
    entry.subentries = {}
    hass = make_hass()

    def _add_subentry(entry_, sub):
        entry_.subentries[sub.subentry_id] = sub
        return True

    hass.config_entries.async_add_subentry = _add_subentry

    coordinator = MagicMock()
    coordinator._emaldo_device_id = "dev_1"

    dev_reg = MagicMock()
    # Fresh device: get_or_create (no subentry) yields a None subentry binding
    dev_reg.async_get_or_create = MagicMock(
        return_value=_device_mock({"entry_1": {None}})
    )
    dev_reg.async_update_device = MagicMock()

    with patch.object(dr, "async_get", return_value=dev_reg):
        asyncio.run(_ensure_device_subentry(hass, entry, coordinator))

    device_sub = next(
        sub
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_TYPE_DEVICE
    )
    assert device_sub.unique_id == DEVICE_SUBENTRY_UNIQUE_ID
    assert device_sub.title == DEVICE_SUBENTRY_LABEL
    assert device_sub.data == {}

    # get_or_create is called WITHOUT config_subentry_id
    assert dev_reg.async_get_or_create.call_args.kwargs["identifiers"] == {
        (DOMAIN, "dev_1")
    }
    assert "config_subentry_id" not in dev_reg.async_get_or_create.call_args.kwargs

    # binding moved to the device subentry via async_update_device (add+remove)
    assert dev_reg.async_update_device.call_args_list[0].kwargs == {
        "add_config_entry_id": entry.entry_id,
        "add_config_subentry_id": device_sub.subentry_id,
    }
    assert dev_reg.async_update_device.call_args_list[1].kwargs == {
        "remove_config_entry_id": entry.entry_id,
        "remove_config_subentry_id": None,
    }


def test_ensure_device_subentry_idempotent():
    """A device already bound to the device subentry is left untouched."""
    from unittest.mock import patch

    import homeassistant.helpers.device_registry as dr

    from custom_components.battery_optimizer import _ensure_device_subentry
    from custom_components.battery_optimizer.const import (
        DEVICE_SUBENTRY_LABEL,
        DEVICE_SUBENTRY_UNIQUE_ID,
        SUBENTRY_TYPE_DEVICE,
    )

    entry = make_entry()
    entry.subentries = {
        "s_dev": ConfigSubentry(
            subentry_id="s_dev",
            subentry_type=SUBENTRY_TYPE_DEVICE,
            title=DEVICE_SUBENTRY_LABEL,
            unique_id=DEVICE_SUBENTRY_UNIQUE_ID,
            data={},
        )
    }
    hass = make_hass()
    dev_reg = MagicMock()
    dev_reg.async_get_or_create = MagicMock(
        return_value=_device_mock({"entry_1": {"s_dev"}})
    )
    dev_reg.async_update_device = MagicMock()

    coordinator = MagicMock()
    coordinator._emaldo_device_id = "dev_1"

    with patch.object(dr, "async_get", return_value=dev_reg):
        asyncio.run(_ensure_device_subentry(hass, entry, coordinator))
        asyncio.run(_ensure_device_subentry(hass, entry, coordinator))

    hass.config_entries.async_add_subentry.assert_not_called()
    assert dev_reg.async_get_or_create.call_count == 2
    # steady state: already bound to the device subentry -> no move call
    dev_reg.async_update_device.assert_not_called()


def test_ensure_device_subentry_moves_from_rule_subentry():
    """A device bound to the default RULE subentry is moved to the device subentry.

    Covers the real-HA scenario behind the 2027.8 deprecation warning: an
    install where the device was bound to the default-rule subentry by an
    earlier version.  The move uses async_update_device, not
    async_get_or_create(config_subentry_id=...) which silently moves devices.
    """
    from unittest.mock import patch

    import homeassistant.helpers.device_registry as dr

    from custom_components.battery_optimizer import _ensure_device_subentry
    from custom_components.battery_optimizer.const import (
        DEVICE_SUBENTRY_LABEL,
        DEVICE_SUBENTRY_UNIQUE_ID,
        SUBENTRY_TYPE_DEVICE,
    )

    entry = make_entry()
    entry.subentries = {
        "s_dev": ConfigSubentry(
            subentry_id="s_dev",
            subentry_type=SUBENTRY_TYPE_DEVICE,
            title=DEVICE_SUBENTRY_LABEL,
            unique_id=DEVICE_SUBENTRY_UNIQUE_ID,
            data={},
        )
    }
    hass = make_hass()
    dev_reg = MagicMock()
    # device currently bound to the default RULE subentry (old install)
    dev_reg.async_get_or_create = MagicMock(
        return_value=_device_mock({"entry_1": {"s_rule"}})
    )
    dev_reg.async_update_device = MagicMock()

    coordinator = MagicMock()
    coordinator._emaldo_device_id = "dev_1"

    with patch.object(dr, "async_get", return_value=dev_reg):
        asyncio.run(_ensure_device_subentry(hass, entry, coordinator))

    calls = dev_reg.async_update_device.call_args_list
    assert calls[0].kwargs == {
        "add_config_entry_id": entry.entry_id,
        "add_config_subentry_id": "s_dev",
    }
    assert calls[1].kwargs == {
        "remove_config_entry_id": entry.entry_id,
        "remove_config_subentry_id": "s_rule",
    }


def test_ensure_device_subentry_handles_none_binding():
    """A device whose subentry binding is {None} is moved to the device subentry.

    ``None`` in ``config_entries_subentries`` means "no subentry" (the
    frontend's plain-entry state); the stale ``None`` binding must be
    removed when moving to the device subentry.
    """
    from unittest.mock import patch

    import homeassistant.helpers.device_registry as dr

    from custom_components.battery_optimizer import _ensure_device_subentry
    from custom_components.battery_optimizer.const import (
        DEVICE_SUBENTRY_LABEL,
        DEVICE_SUBENTRY_UNIQUE_ID,
        SUBENTRY_TYPE_DEVICE,
    )

    entry = make_entry()
    entry.subentries = {
        "s_dev": ConfigSubentry(
            subentry_id="s_dev",
            subentry_type=SUBENTRY_TYPE_DEVICE,
            title=DEVICE_SUBENTRY_LABEL,
            unique_id=DEVICE_SUBENTRY_UNIQUE_ID,
            data={},
        )
    }
    hass = make_hass()
    dev_reg = MagicMock()
    dev_reg.async_get_or_create = MagicMock(
        return_value=_device_mock({"entry_1": {None}})
    )
    dev_reg.async_update_device = MagicMock()

    coordinator = MagicMock()
    coordinator._emaldo_device_id = "dev_1"

    with patch.object(dr, "async_get", return_value=dev_reg):
        asyncio.run(_ensure_device_subentry(hass, entry, coordinator))

    calls = dev_reg.async_update_device.call_args_list
    assert calls[0].kwargs == {
        "add_config_entry_id": entry.entry_id,
        "add_config_subentry_id": "s_dev",
    }
    assert calls[1].kwargs == {
        "remove_config_entry_id": entry.entry_id,
        "remove_config_subentry_id": None,
    }



def test_rule_schema_serializes_for_frontend():
    """The form schema must survive voluptuous_serialize conversion —
    HA's flow framework converts every submitted schema to JSON and 500s
    on unconvertible validators (real-HA bug: ensure_list/Any(None,last)).
    Each level renders only its own fields; level/action live on the menu
    routes, so neither appears in the form.
    """
    import voluptuous_serialize
    from homeassistant.helpers import config_validation as cv

    from custom_components.battery_optimizer.config_flow import _rule_schema

    # weekday charge: days but no date fields
    schema = _rule_schema({}, action="charge", level="weekday")
    out = voluptuous_serialize.convert(
        schema, custom_serializer=cv.custom_serializer
    )
    # every field converts; the weekdays field is a modern selector
    entries = {entry["name"]: entry for entry in out}
    assert set(entries) == {
        "label", "days", "start_time", "end_time", "soc_target", "pv_sell",
    }
    days_entry = entries["days"]
    assert days_entry.get("type") is None  # selector-based, not legacy type
    assert "select" in days_entry.get("selector", {})

    # date level: DateSelector fields survive serialization (None-first
    # vol.Any is the classic 500 source) and render as date selectors
    date_schema = _rule_schema({}, action="idle", level="date")
    date_out = voluptuous_serialize.convert(
        date_schema, custom_serializer=cv.custom_serializer
    )
    date_entries = {entry["name"]: entry for entry in date_out}
    assert set(date_entries) == {
        "label", "start_date", "end_date", "start_time", "end_time", "pv_sell",
    }
    assert "date" in date_entries["start_date"].get("selector", {})
    assert "date" in date_entries["end_date"].get("selector", {})

    # default level: neither days nor dates
    default_schema = _rule_schema({}, action="optimizer", level="default")
    default_out = voluptuous_serialize.convert(
        default_schema, custom_serializer=cv.custom_serializer
    )
    assert set(entry["name"] for entry in default_out) == {
        "label", "start_time", "end_time", "pv_sell",
    }

    # and the schema accepts the frontend's string day values
    assert schema({"days": ["0", "3"], "start_time": "07:00",
                   "end_time": "17:00",
                   "soc_target": None, "pv_sell": "inherit", "label": ""})
    # edit path: stored int day defaults convert to selector strings
    # (SelectSelector only accepts str option values, so ints validate
    # only through the defaults conversion, not the field itself)
    edit = _rule_schema({"days": [0, 3]}, action="charge", level="weekday")
    form_defaults = {
        key.schema: key.default()
        for key in edit.schema
        if key.default is not None
    }
    assert form_defaults["days"] == ["0", "3"]


def test_rule_schema_idle_has_no_soc_target_field():
    """Idle (and original/optimizer) schemas omit the soc_target field."""
    import voluptuous_serialize
    from homeassistant.helpers import config_validation as cv

    from custom_components.battery_optimizer.config_flow import _rule_schema

    for action in ("idle", "original", "optimizer"):
        schema = _rule_schema({}, action=action)
        out = voluptuous_serialize.convert(
            schema, custom_serializer=cv.custom_serializer
        )
        entries = {entry["name"]: entry for entry in out}
        assert "soc_target" not in entries, action
        assert "soc_target" not in {
            key.schema for key in schema.schema
        }, action


def test_rule_schema_hides_pv_sell_for_non_charge():
    from custom_components.battery_optimizer.config_flow import _rule_schema

    charge_schema = _rule_schema({}, action="charge")
    assert "pv_sell" in charge_schema.schema

    idle_schema = _rule_schema({}, action="idle")
    assert "pv_sell" not in idle_schema.schema

    optimizer_schema = _rule_schema({}, action="optimizer")
    assert "pv_sell" not in optimizer_schema.schema

    discharge_schema = _rule_schema({}, action="discharge")
    assert "pv_sell" in discharge_schema.schema


def test_rule_enabled_defaults_true():
    from custom_components.battery_optimizer.rules import rule_from_data

    # missing key -> defaults True (backward compatible)
    r1 = rule_from_data({"level": "weekday", "action": "charge", "start_time": "07:00", "end_time": "17:00"})
    assert r1.enabled is True

    r2 = rule_from_data({"level": "weekday", "action": "charge", "start_time": "07:00", "end_time": "17:00", "enabled": False})
    assert r2.enabled is False


def test_read_user_rules_skips_disabled():
    from custom_components.battery_optimizer.coordinator import BatteryOptimizerCoordinator
    from homeassistant.config_entries import ConfigSubentry, ConfigEntry
    from unittest.mock import MagicMock

    enabled = ConfigSubentry(
        subentry_id="s_on", subentry_type="rule",
        data={"level": "weekday", "days": [0], "start_time": "07:00",
              "end_time": "17:00", "action": "charge", "soc_target": 80,
              "pv_sell": "inherit", "label": "On", "enabled": True},
    )
    disabled = ConfigSubentry(
        subentry_id="s_off", subentry_type="rule",
        data={"level": "weekday", "days": [1], "start_time": "08:00",
              "end_time": "18:00", "action": "charge", "soc_target": 80,
              "pv_sell": "inherit", "label": "Off", "enabled": False},
    )
    default_rule = ConfigSubentry(
        subentry_id="s_def", subentry_type="rule",
        data={"level": "default", "days": [], "start_time": "00:00",
              "end_time": "24:00", "action": "optimizer", "soc_target": None,
              "pv_sell": "inherit", "label": "Default", "enabled": True},
    )
    entry = MagicMock(spec=ConfigEntry)
    entry.subentries = {"s_on": enabled, "s_off": disabled, "s_def": default_rule}

    coord = object.__new__(BatteryOptimizerCoordinator)
    coord._entry = entry

    rules = coord._read_user_rules()
    ids = {r.label for r in rules}
    assert "Off" not in ids
    assert "On" in ids
    assert "Default" in ids


def test_rule_schema_has_enabled_checkbox():
    from custom_components.battery_optimizer.config_flow import _rule_schema

    schema = _rule_schema({}, action="charge")
    assert "enabled" in schema.schema


def test_set_rule_enabled_updates_subentry_and_reruns():
    from custom_components.battery_optimizer.coordinator import BatteryOptimizerCoordinator
    from homeassistant.config_entries import ConfigSubentry, ConfigEntry
    from unittest.mock import MagicMock, AsyncMock
    import asyncio

    sub = ConfigSubentry(
        subentry_id="s1", subentry_type="rule",
        data={"level": "weekday", "days": [0], "start_time": "07:00",
              "end_time": "17:00", "action": "charge", "soc_target": 80,
              "pv_sell": "inherit", "label": "R", "enabled": True},
    )
    entry = MagicMock(spec=ConfigEntry)
    entry.subentries = {"s1": sub}
    updated = {}

    async def fake_update(entry_, sub_, data=None):
        updated["data"] = data

    coord = object.__new__(BatteryOptimizerCoordinator)
    coord._entry = entry
    coord.hass = MagicMock()
    coord.hass.config_entries = MagicMock()
    coord.hass.config_entries.async_update_subentry = AsyncMock(side_effect=fake_update)
    coord.run_optimizer = AsyncMock()

    asyncio.get_event_loop().run_until_complete(coord.set_rule_enabled("s1", False))

    assert updated["data"]["enabled"] is False
    coord.run_optimizer.assert_awaited_once()


def test_rule_enabled_switch_reflects_and_toggles():
    from custom_components.battery_optimizer.switch import RuleEnabledSwitch
    from custom_components.battery_optimizer.coordinator import BatteryOptimizerCoordinator
    from homeassistant.config_entries import ConfigSubentry, ConfigEntry
    from unittest.mock import MagicMock, AsyncMock
    import asyncio

    sub = ConfigSubentry(
        subentry_id="s1", subentry_type="rule",
        data={"level": "weekday", "days": [0], "start_time": "07:00",
              "end_time": "17:00", "action": "charge", "soc_target": 80,
              "pv_sell": "inherit", "label": "Morning", "enabled": True},
    )
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "entry_1"
    coord = MagicMock(spec=BatteryOptimizerCoordinator)
    coord.set_rule_enabled = AsyncMock()

    sw = RuleEnabledSwitch(coord, entry, sub)
    assert sw.is_on is True
    assert sw.unique_id == "entry_1_s1_enabled"

    asyncio.get_event_loop().run_until_complete(sw.async_turn_off())
    coord.set_rule_enabled.assert_awaited_once_with("s1", False)


def test_reconfigure_persists_enabled_false():
    """Unchecking the Enabled checkbox in the rule editor must persist.

    Regression (v0.3.6b): ``_detail_step`` built the saved subentry data
    WITHOUT the ``enabled`` key, so editing any rule reset its enabled
    state to the default True — the checkbox appeared re-enabled and the
    plan kept applying the rule.  Date-level (no days field) mirrors the
    reported case: a same-day 08:30-09:30 rule disabled via the editor.
    """
    entry = make_entry()
    entry.subentries = {
        "d1": ConfigSubentry(
            subentry_id="d1",
            subentry_type="rule",
            title="Today",
            unique_id="d1",
            data={
                "level": "date",
                "days": [],
                "start_date": "2026-08-26",
                "end_date": "2026-08-26",
                "start_time": "08:30",
                "end_time": "09:30",
                "action": "charge",
                "soc_target": 80,
                "pv_sell": "inherit",
                "label": "Today",
                "enabled": True,
            },
        )
    }
    flow = _make_reconfigure_flow(entry, "d1")

    # reconfigure: level menu -> date -> action menu -> charge detail form
    menu = asyncio.run(flow.async_step_reconfigure(None))
    assert menu["type"] == "menu"
    action_menu = asyncio.run(flow.async_step_date(None))
    assert action_menu["type"] == "menu"
    form = asyncio.run(flow.async_step_charge(None))
    assert form["type"] == "form"
    assert form["step_id"] == "detail_charge"

    # uncheck the Enabled checkbox, keep everything else unchanged
    user_input = {
        "start_date": "2026-08-26",
        "end_date": "2026-08-26",
        "start_time": "08:30",
        "end_time": "09:30",
        "soc_target": 80,
        "pv_sell": "inherit",
        "label": "Today",
        "enabled": False,
    }
    result = asyncio.run(flow.async_step_charge(user_input))
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    call = flow.hass.config_entries.async_update_subentry.call_args
    assert call.kwargs["data"]["start_time"] == "08:30"
    assert call.kwargs["data"]["enabled"] is False


def test_forced_sell_options_roundtrip():
    """Options flow stores 4 forced-sell keys; coordinator maps defaults."""
    from custom_components.battery_optimizer.config_flow import (
        BatteryOptimizerOptionsFlow,
    )
    from custom_components.battery_optimizer.const import (
        CONF_FORCED_SELL_ENABLED,
        CONF_FORCED_SELL_MAX_KWH_PD,
        CONF_FORCED_SELL_MIN_PROFIT,
        CONF_FORCED_SELL_MIN_PRICE,
        DEFAULT_FORCED_SELL_ENABLED,
        DEFAULT_FORCED_SELL_MAX_KWH_PD,
        DEFAULT_FORCED_SELL_MIN_PROFIT,
        DEFAULT_FORCED_SELL_MIN_PRICE,
    )
    from custom_components.battery_optimizer.coordinator import (
        BatteryOptimizerCoordinator,
    )

    hass = make_hass()
    hass.data = {}
    flow = BatteryOptimizerOptionsFlow(hass, make_entry())
    user_input = {
        CONF_FORCED_SELL_ENABLED: True,
        CONF_FORCED_SELL_MAX_KWH_PD: 6.0,
        CONF_FORCED_SELL_MIN_PROFIT: 0.05,
        CONF_FORCED_SELL_MIN_PRICE: 0.04,
    }
    result = asyncio.run(flow.async_step_init(user_input))
    assert result["type"] == "create_entry"
    assert result["data"] == user_input

    # Coordinator mapping layer: values land on BatteryConfig...
    coord = object.__new__(BatteryOptimizerCoordinator)
    coord._auto_base_load_value = None
    coord.config = {**user_input}
    bc = coord._build_battery_config()
    assert bc.forced_sell_enabled is True
    assert bc.forced_sell_max_kwh_pd == 6.0
    assert bc.forced_sell_min_profit == 0.05
    assert bc.forced_sell_min_price == 0.04

    # ...and defaults apply when keys are omitted.
    coord.config = {}
    bc = coord._build_battery_config()
    assert bc.forced_sell_enabled == DEFAULT_FORCED_SELL_ENABLED
    assert bc.forced_sell_max_kwh_pd == DEFAULT_FORCED_SELL_MAX_KWH_PD
    assert bc.forced_sell_min_profit == DEFAULT_FORCED_SELL_MIN_PROFIT
    assert bc.forced_sell_min_price == DEFAULT_FORCED_SELL_MIN_PRICE
