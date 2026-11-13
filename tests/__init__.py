"""Test package — loads the real optimizer module without HA imports.

The production ``optimizer.py`` does ``from .const import ...`` (relative
import), so it is exec'd into a fabricated package ``bopt_pkg`` whose
``const`` submodule is the real ``const.py``.  Tests always exercise the
live source files — no copies, no drift.
"""

from __future__ import annotations

import datetime as _dt
import sys
import types
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = _REPO_ROOT / "custom_components" / "battery_optimizer"

_PKG_NAME = "bopt_pkg"


def load_optimizer():
    """Return (optimizer_module, const_module) loaded from the repo source."""
    if _PKG_NAME in sys.modules:
        return sys.modules[_PKG_NAME].optimizer, sys.modules[_PKG_NAME].const

    pkg = types.ModuleType(_PKG_NAME)
    pkg.__path__ = []  # required so submodule relative imports resolve
    sys.modules[_PKG_NAME] = pkg

    def _exec(target: str, filename: Path):
        spec = spec_from_file_location(target, str(filename))
        mod = module_from_spec(spec)
        sys.modules[target] = mod
        spec.loader.exec_module(mod)
        return mod

    const = _exec(f"{_PKG_NAME}.const", _SRC_DIR / "const.py")
    pkg.const = const
    optimizer = _exec(f"{_PKG_NAME}.optimizer", _SRC_DIR / "optimizer.py")
    pkg.optimizer = optimizer
    return optimizer, const


def load_solar_scale():
    """Return (solar_scale_module, const_module) loaded from the repo source.

    ``solar_scale.py`` does ``from .const import ...`` (relative import), so it
    must run inside the fabricated ``bopt_pkg`` alongside the real ``const``.
    """
    if _PKG_NAME in sys.modules and hasattr(sys.modules[_PKG_NAME], "solar_scale"):
        return sys.modules[_PKG_NAME].solar_scale, sys.modules[_PKG_NAME].const

    load_optimizer()  # builds bopt_pkg + const
    pkg = sys.modules[_PKG_NAME]

    def _exec(target: str, filename: Path):
        spec = spec_from_file_location(target, str(filename))
        mod = module_from_spec(spec)
        sys.modules[target] = mod
        spec.loader.exec_module(mod)
        return mod

    solar_scale = _exec(f"{_PKG_NAME}.solar_scale", _SRC_DIR / "solar_scale.py")
    pkg.solar_scale = solar_scale
    return solar_scale, pkg.const


def load_emaldo_solar():
    """Return (emaldo_solar_module, const_module) loaded from the repo source.

    ``emaldo_solar.py`` does ``from .const import SLOTS_PER_DAY`` (relative
    import), so it runs inside the fabricated ``bopt_pkg`` next to the real
    ``const``.
    """
    if _PKG_NAME in sys.modules and hasattr(sys.modules[_PKG_NAME], "emaldo_solar"):
        return (
            sys.modules[_PKG_NAME].emaldo_solar,
            sys.modules[_PKG_NAME].const,
        )

    load_optimizer()  # builds bopt_pkg + const
    pkg = sys.modules[_PKG_NAME]

    spec = spec_from_file_location(
        f"{_PKG_NAME}.emaldo_solar", str(_SRC_DIR / "emaldo_solar.py")
    )
    mod = module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    pkg.emaldo_solar = mod
    return mod, pkg.const


def load_solar_actual():
    """Return (solar_actual_module, const_module) loaded from the repo source.

    ``solar_actual.py`` is a pure module (no relative imports), but it is
    loaded inside the fabricated ``bopt_pkg`` for consistency.
    """
    if _PKG_NAME in sys.modules and hasattr(sys.modules[_PKG_NAME], "solar_actual"):
        return sys.modules[_PKG_NAME].solar_actual, sys.modules[_PKG_NAME].const

    load_optimizer()  # builds bopt_pkg + const
    pkg = sys.modules[_PKG_NAME]

    def _exec(target: str, filename: Path):
        spec = spec_from_file_location(target, str(filename))
        mod = module_from_spec(spec)
        sys.modules[target] = mod
        spec.loader.exec_module(mod)
        return mod

    solar_actual = _exec(f"{_PKG_NAME}.solar_actual", _SRC_DIR / "solar_actual.py")
    pkg.solar_actual = solar_actual
    return solar_actual, pkg.const


def load_optional_value():
    """Return (optional_value_module, const_module) loaded from the repo source.

    ``optional_value.py`` is a pure module (no relative imports, no Home
    Assistant imports), but it is loaded inside the fabricated ``bopt_pkg`` for
    consistency with the other pure modules.
    """
    if _PKG_NAME in sys.modules and hasattr(sys.modules[_PKG_NAME], "optional_value"):
        return sys.modules[_PKG_NAME].optional_value, sys.modules[_PKG_NAME].const

    load_optimizer()  # builds bopt_pkg + const
    pkg = sys.modules[_PKG_NAME]

    def _exec(target: str, filename: Path):
        spec = spec_from_file_location(target, str(filename))
        mod = module_from_spec(spec)
        sys.modules[target] = mod
        spec.loader.exec_module(mod)
        return mod

    optional_value = _exec(f"{_PKG_NAME}.optional_value", _SRC_DIR / "optional_value.py")
    pkg.optional_value = optional_value
    return optional_value, pkg.const


def load_marker_io():
    """Return (marker_io_module, const_module) loaded from the repo source.

    ``marker_io.py`` is a pure module (no relative imports, no Home Assistant
    imports), but it is loaded inside the fabricated ``bopt_pkg`` for consistency
    with the other pure modules.
    """
    if _PKG_NAME in sys.modules and hasattr(sys.modules[_PKG_NAME], "marker_io"):
        return sys.modules[_PKG_NAME].marker_io, sys.modules[_PKG_NAME].const

    load_optimizer()  # builds bopt_pkg + const
    pkg = sys.modules[_PKG_NAME]

    def _exec(target: str, filename: Path):
        spec = spec_from_file_location(target, str(filename))
        mod = module_from_spec(spec)
        sys.modules[target] = mod
        spec.loader.exec_module(mod)
        return mod

    marker_io = _exec(f"{_PKG_NAME}.marker_io", _SRC_DIR / "marker_io.py")
    pkg.marker_io = marker_io
    return marker_io, pkg.const


def load_sale_state():
    """Return (sale_state_module, const_module) loaded from the repo source.

    ``sale_state.py`` is a pure module (no relative imports, no Home Assistant
    imports), but it is loaded inside the fabricated ``bopt_pkg`` for consistency
    with the other pure modules.
    """
    if _PKG_NAME in sys.modules and hasattr(sys.modules[_PKG_NAME], "sale_state"):
        return sys.modules[_PKG_NAME].sale_state, sys.modules[_PKG_NAME].const

    load_optimizer()  # builds bopt_pkg + const
    pkg = sys.modules[_PKG_NAME]

    def _exec(target: str, filename: Path):
        spec = spec_from_file_location(target, str(filename))
        mod = module_from_spec(spec)
        sys.modules[target] = mod
        spec.loader.exec_module(mod)
        return mod

    sale_state = _exec(f"{_PKG_NAME}.sale_state", _SRC_DIR / "sale_state.py")
    pkg.sale_state = sale_state
    return sale_state, pkg.const


def load_rules():
    """Return (rules_module, const_module) loaded from the repo source.

    ``rules.py`` does ``from .const import ...`` (relative import), so it
    must run inside the fabricated ``bopt_pkg`` alongside the real const.
    """
    if _PKG_NAME in sys.modules and hasattr(sys.modules[_PKG_NAME], "rules"):
        return sys.modules[_PKG_NAME].rules, sys.modules[_PKG_NAME].const

    load_optimizer()  # builds bopt_pkg + const
    pkg = sys.modules[_PKG_NAME]

    def _exec(target: str, filename: Path):
        spec = spec_from_file_location(target, str(filename))
        mod = module_from_spec(spec)
        sys.modules[target] = mod
        spec.loader.exec_module(mod)
        return mod

    rules = _exec(f"{_PKG_NAME}.rules", _SRC_DIR / "rules.py")
    pkg.rules = rules
    return rules, pkg.const


def load_solar_balance():
    """Return solar_balance_module loaded from the repo source.

    ``solar_balance.py`` is a pure module (no relative imports), loaded
    inside the fabricated ``bopt_pkg`` for consistency.
    """
    if _PKG_NAME in sys.modules and hasattr(sys.modules[_PKG_NAME], "solar_balance"):
        return sys.modules[_PKG_NAME].solar_balance

    load_optimizer()  # builds bopt_pkg
    pkg = sys.modules[_PKG_NAME]

    spec = spec_from_file_location(
        f"{_PKG_NAME}.solar_balance", str(_SRC_DIR / "solar_balance.py")
    )
    mod = module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    pkg.solar_balance = mod
    return mod


def _install_fake_homeassistant():
    """Stub homeassistant in sys.modules so coordinator.py / sensor.py load.

    The production modules import several homeassistant submodules at module
    load (including ``components.sensor`` and ``helpers.entity_platform`` used
    by sensor.py).  The tests exercise pure logic with fakes, so only the
    import surface needs to exist — no real HA behaviour.

    Returns True if a stub was installed (caller must remove it afterwards),
    False if a real homeassistant is already present (leave it alone).
    """
    if "homeassistant" in sys.modules:
        return False

    ha = types.ModuleType("homeassistant")

    util = types.ModuleType("homeassistant.util")
    dt = types.ModuleType("homeassistant.util.dt")
    dt.dt_util = types.SimpleNamespace(now=lambda: None)
    dt.now = lambda: _dt.datetime(2026, 1, 1, 0, 0, 0)
    # Fixed +01:00 zone: fixtures can write "+01:00" timestamps and read the
    # wall clock back as local time, with no IANA database dependency.
    dt.get_time_zone = lambda tz: _dt.timezone(_dt.timedelta(hours=1))
    util.dt = dt

    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.ConfigEntry = object
    core.CALLBACK_TYPE = object
    core.callback = lambda f: f

    const = types.ModuleType("homeassistant.const")
    const.EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
    const.EntityCategory = types.SimpleNamespace(DIAGNOSTIC="diagnostic")

    ce = types.ModuleType("homeassistant.config_entries")
    ce.ConfigEntry = object

    components = types.ModuleType("homeassistant.components")
    sensor_mod = types.ModuleType("homeassistant.components.sensor")
    sensor_mod.SensorEntity = object
    sensor_mod.SensorDeviceClass = types.SimpleNamespace(
        TIMESTAMP="timestamp", MONETARY="monetary"
    )
    sensor_mod.SensorStateClass = types.SimpleNamespace(MEASUREMENT="measurement")
    components.sensor = sensor_mod

    helpers = types.ModuleType("homeassistant.helpers")
    event = types.ModuleType("homeassistant.helpers.event")
    for name in (
        "async_call_later",
        "async_track_state_change_event",
        "async_track_time_change",
        "async_track_time_interval",
    ):
        setattr(event, name, lambda *a, **k: None)
    er_mod = types.ModuleType("homeassistant.helpers.entity_registry")
    uc = types.ModuleType("homeassistant.helpers.update_coordinator")

    class DataUpdateCoordinator:
        __class_getitem__ = classmethod(lambda cls, _: cls)

    uc.DataUpdateCoordinator = DataUpdateCoordinator

    class _CoordEntity:
        __class_getitem__ = classmethod(lambda cls, _: cls)

    uc.CoordinatorEntity = _CoordEntity

    ep = types.ModuleType("homeassistant.helpers.entity_platform")
    ep.AddEntitiesCallback = object

    # coordinator.py does ``from homeassistant.helpers import device_registry
    # as dr`` and only ever calls ``dr.async_get(hass)`` (the returned
    # registry is used inside ``_resolve_emaldo_registry_device_id``, which no
    # test exercises).  None keeps that path inert: an unresolvable registry
    # is the same as a device that is not registered yet.
    dr_mod = types.ModuleType("homeassistant.helpers.device_registry")
    dr_mod.async_get = lambda hass: None

    # coordinator.py does ``from homeassistant.helpers import issue_registry
    # as ir`` for the locale-money repair issues; no test exercises them.
    ir_mod = types.ModuleType("homeassistant.helpers.issue_registry")
    ir_mod.IssueSeverity = types.SimpleNamespace(WARNING="warning")
    ir_mod.async_create_issue = lambda *a, **k: None
    ir_mod.async_delete_issue = lambda *a, **k: None
    helpers.issue_registry = ir_mod
    sys.modules["homeassistant.helpers.issue_registry"] = ir_mod
    helpers.event = event
    helpers.entity_registry = er_mod
    helpers.update_coordinator = uc
    helpers.entity_platform = ep
    helpers.device_registry = dr_mod

    ha.util = util
    ha.core = core
    ha.const = const
    ha.config_entries = ce
    ha.components = components
    ha.helpers = helpers

    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.util"] = util
    sys.modules["homeassistant.util.dt"] = dt
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.const"] = const
    sys.modules["homeassistant.config_entries"] = ce
    sys.modules["homeassistant.components"] = components
    sys.modules["homeassistant.components.sensor"] = sensor_mod
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.event"] = event
    sys.modules["homeassistant.helpers.entity_registry"] = er_mod
    sys.modules["homeassistant.helpers.update_coordinator"] = uc
    sys.modules["homeassistant.helpers.entity_platform"] = ep
    sys.modules["homeassistant.helpers.device_registry"] = dr_mod

    return True


def load_coordinator():
    """Return (coordinator_module, const_module) loaded from the repo source.

    ``coordinator.py`` imports homeassistant at module load, so a stubbed
    ``homeassistant`` package is installed first.  The fabricated ``bopt_pkg``
    carries the real sibling pure modules (const/optimizer/rules/solar_*/
    runtime_state) that coordinator imports relatively.
    """
    if _PKG_NAME in sys.modules and hasattr(sys.modules[_PKG_NAME], "coordinator"):
        pkg = sys.modules[_PKG_NAME]
        return pkg.coordinator, pkg.const

    load_optimizer()  # builds bopt_pkg + const + optimizer
    pkg = sys.modules[_PKG_NAME]

    if not hasattr(pkg, "solar_actual"):
        load_solar_actual()
    if not hasattr(pkg, "rules"):
        load_rules()
    if not hasattr(pkg, "solar_scale"):
        load_solar_scale()
    if not hasattr(pkg, "marker_io"):
        load_marker_io()
    if not hasattr(pkg, "sale_state"):
        load_sale_state()

    def _exec(target, filename):
        spec = spec_from_file_location(target, str(filename))
        mod = module_from_spec(spec)
        sys.modules[target] = mod
        spec.loader.exec_module(mod)
        return mod

    for mod_name in (
        "money", "solar_regime", "runtime_state", "cost_history", "plan_export",
        "push_state", "emaldo_solar",
    ):
        if not hasattr(pkg, mod_name):
            _exec(f"{_PKG_NAME}.{mod_name}", _SRC_DIR / f"{mod_name}.py")

    installed = _install_fake_homeassistant()

    coordinator = _exec(f"{_PKG_NAME}.coordinator", _SRC_DIR / "coordinator.py")
    pkg.coordinator = coordinator

    # Drop the stubbed homeassistant so it cannot leak into other tests that
    # import the real package (which correctly require a real HA install).
    # The coordinator module already bound its HA names at exec time.  If a
    # real homeassistant was already present (CI), leave it untouched.
    if installed:
        _remove_fake_homeassistant()

    return coordinator, pkg.const


def load_sensor():
    """Return the sensor module loaded from the repo source.

    Builds on ``load_coordinator`` (which fabricates ``bopt_pkg`` and stubs
    homeassistant for coordinator.py), then extends the stub with the extra
    submodules sensor.py imports, execs the live sensor.py into ``bopt_pkg``,
    and removes the stub so it cannot leak into other tests.
    """
    if _PKG_NAME in sys.modules and hasattr(sys.modules[_PKG_NAME], "sensor"):
        return sys.modules[_PKG_NAME].sensor

    load_coordinator()  # builds bopt_pkg + coordinator + friends
    pkg = sys.modules[_PKG_NAME]

    if not hasattr(pkg, "solar_balance"):
        load_solar_balance()

    installed = _install_fake_homeassistant()
    try:
        spec = spec_from_file_location(
            f"{_PKG_NAME}.sensor", str(_SRC_DIR / "sensor.py")
        )
        mod = module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    finally:
        if installed:
            _remove_fake_homeassistant()

    pkg.sensor = mod
    return mod


def _remove_fake_homeassistant():
    """Remove the stubbed homeassistant package from sys.modules."""
    for name in list(sys.modules):
        if name == "homeassistant" or name.startswith("homeassistant."):
            del sys.modules[name]
