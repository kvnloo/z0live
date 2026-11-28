import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from meetings import DEFAULTS, make
from meetings.server import config_yaml


class PluginTest(unittest.TestCase):
    def test_plugin_load_registers_meetings_tools(self):
        path = Path(__file__).resolve().parents[1] / '__init__.py'
        spec = importlib.util.spec_from_file_location('_meetings_test_plugin', path)
        assert spec is not None and spec.loader is not None
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = plugin
        self.addCleanup(sys.modules.pop, spec.name)
        spec.loader.exec_module(plugin)
        tools = {}

        class Context:
            def get_config(self, name, default):
                return default

            def register_tool(self, **kwargs):
                tools[kwargs['name']] = kwargs

            def register_command(self, *args, **kwargs):
                pass

        plugin.register(Context())
        self.assertEqual({name for name in tools if name.startswith('meetings_')},
                         {'meetings_start', 'meetings_invite', 'meetings_status', 'meetings_leave'})

    def test_agent_cannot_mint_owner_invite(self):
        with patch('meetings.Meetings') as klass:
            klass.return_value.invite.return_value = {'ok': True}
            result = json.loads(make(DEFAULTS.__getitem__)['meetings_invite']({'role': 'owner'}))
            self.assertFalse(result['ok'])
            klass.return_value.invite.assert_not_called()

    def test_start_with_bot_requires_configuration(self):
        with patch('meetings.Meetings') as klass:
            klass.return_value.start.return_value = {'ok': True}
            result = json.loads(make(DEFAULTS.__getitem__)['meetings_start']({}))
            self.assertFalse(result['ok'])
            klass.return_value.start.assert_not_called()

    def test_tailnet_mode_refuses_non_cgnat_ip(self):
        with self.assertRaises(ValueError):
            config_yaml('tailnet', ip='100.2.1.1')


if __name__ == '__main__':
    unittest.main()
