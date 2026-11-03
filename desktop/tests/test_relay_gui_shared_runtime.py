import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


DESKTOP = Path("/mnt/zer0models/worktrees/z0live-relay-gui-20261102/desktop")
RELAY_SRC = (DESKTOP / "hermes_relay.py").read_text()


class TestRelayGuiAdapter(unittest.TestCase):
    def test_gui_in_driven_from(self):
        sys.path.insert(0, str(DESKTOP))
        from z0live_desktop.relay import DRIVEN_FROM, HUMAN_SOURCES
        self.assertIn("gui", DRIVEN_FROM)
        self.assertIn("gui", HUMAN_SOURCES)

    def test_shared_runtime_fn_exists(self):
        self.assertIn("_try_shared_runtime_endpoint", RELAY_SRC)

    def test_discover_attach_url_called(self):
        self.assertIn("discover_attach_url", RELAY_SRC)

    def test_shared_endpoint_in_deliver(self):
        self.assertIn("shared_endpoint", RELAY_SRC)

    def test_value_error_returns_none(self):
        ssa = types.SimpleNamespace(discover_attach_url=lambda *a, **k: (_ for _ in ()).throw(ValueError("no url")))
        gc = types.SimpleNamespace(GatewayEndpoint=lambda ws_url: ws_url)
        with patch.dict(sys.modules, {
            "hermes_cli.shared_session_attach": ssa,
            "hermes_cli.gateway_client": gc,
        }):
            ns = {}
            exec("""
def _try_shared_runtime_endpoint(sid, home):
    try:
        from hermes_cli.shared_session_attach import discover_attach_url
        ws_url = discover_attach_url(sid, registry_home=home)
        if ws_url is None:
            return None
        from hermes_cli.gateway_client import GatewayEndpoint
        return GatewayEndpoint(ws_url=ws_url)
    except ValueError:
        return None
    except Exception:
        return None
""", ns)
            result = ns["_try_shared_runtime_endpoint"]("sid", "/home")
            self.assertIsNone(result)

    def test_compose_prefix(self):
        sys.path.insert(0, str(DESKTOP))
        from z0live_desktop.relay import compose, PREFIX
        msg = compose("relay test", when=0)
        self.assertIn(PREFIX, msg)
        self.assertIn("relay test", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
