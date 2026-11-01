"""Offline coordinator tests: only Home Assistant services are stubbed."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


def load_coordinator():
    component = Path(__file__).resolve().parents[1] / 'custom_components' / 'battery_optimizer'
    package = types.ModuleType('_battery_optimizer_tests')
    package.__path__ = [str(component)]
    modules = {'_battery_optimizer_tests': package}
    for name in (
        'homeassistant', 'homeassistant.util', 'homeassistant.util.dt',
        'homeassistant.config_entries', 'homeassistant.const', 'homeassistant.core',
        'homeassistant.helpers', 'homeassistant.helpers.event',
        'homeassistant.helpers.device_registry', 'homeassistant.helpers.entity_registry',
        'homeassistant.helpers.issue_registry', 'homeassistant.helpers.update_coordinator',
    ):
        module = types.ModuleType(name)
        module.__path__ = []
        module.__getattr__ = lambda attribute: Mock(name=attribute)
        modules[name] = module
    modules['homeassistant.core'].callback = lambda fn: fn
    class CoordinatorBase:
        def __class_getitem__(cls, item):
            return cls
    modules['homeassistant.helpers.update_coordinator'].DataUpdateCoordinator = CoordinatorBase
    spec = importlib.util.spec_from_file_location('_battery_optimizer_tests.coordinator', component / 'coordinator.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


MODULE = load_coordinator()


class StartupRetryTests(unittest.TestCase):
    def coordinator(self):
        coordinator = MODULE.BatteryOptimizerCoordinator.__new__(MODULE.BatteryOptimizerCoordinator)
        coordinator.hass = Mock()
        coordinator._unsub_startup = None
        coordinator._startup_attempts = 0
        coordinator._startup_retry_delay_s = coordinator._STARTUP_RETRY_DELAY_S
        coordinator._ran_this_session = False
        coordinator._get_battery_soc = Mock(return_value=None)
        coordinator.run_optimizer = Mock(return_value=object())
        return coordinator

    def test_delayed_sensor_still_starts_after_more_than_four_attempts(self):
        coordinator = self.coordinator()
        with patch.object(MODULE, 'async_call_later', return_value=Mock()) as later:
            for _ in range(8):
                coordinator._startup_callback(None)
            coordinator._get_battery_soc.return_value = 50.0
            coordinator._startup_callback(None)
        coordinator.run_optimizer.assert_called_once_with(reason='startup', force=True)
        coordinator.hass.async_create_task.assert_called_once_with(
            coordinator.run_optimizer.return_value
        )
        self.assertEqual(later.call_count, 8)
        self.assertEqual([call.args[1] for call in later.call_args_list],
                         [30, 60, 120, 240, 300, 300, 300, 300])

    def test_success_elsewhere_stops_the_retry_chain(self):
        coordinator = self.coordinator()
        coordinator._ran_this_session = True
        with patch.object(MODULE, 'async_call_later') as later:
            coordinator._startup_callback(None)
        later.assert_not_called()
        coordinator.run_optimizer.assert_not_called()

    def test_shutdown_cancels_the_pending_retry(self):
        coordinator = self.coordinator()
        coordinator._unsub_pv_transitions = []
        coordinator._unsub_listeners = []
        coordinator._unsub_guard = None
        coordinator._unsub_ha_started = None
        coordinator._sell_back_to_grid_saved = False
        cancel = Mock()
        with patch.object(MODULE, 'async_call_later', return_value=cancel):
            coordinator._startup_callback(None)
        coordinator.async_shutdown()
        cancel.assert_called_once_with()
        self.assertIsNone(coordinator._unsub_startup)


if __name__ == '__main__':
    unittest.main()
