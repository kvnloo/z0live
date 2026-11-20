"""The repo-root Hermes plugin: status reads the stack's files; configure validates before it writes. No network."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("z0live_plugin", ROOT / "__init__.py")
z = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(z)


def cfg_for(tmp):
    paths = {"lanes_file": tmp / "lanes.json", "tiers_file": tmp / "tiers.json", "voices_file": tmp / "voices.json",
             "traces_dir": tmp / "traces", "lane_url": "http://127.0.0.1:9", "z0live_repo": tmp / "repo"}
    return lambda key: str(paths[key])


def test_status_reports_engine_guest_and_latest_call(tmp_path):
    (tmp_path / "lanes.json").write_text(json.dumps({"lanes": {"voice": {"actor": "grok_voice"}}}))
    (tmp_path / "voices.json").write_text(json.dumps({"guest": "grok"}))
    day = tmp_path / "traces" / "2026-11-19"
    day.mkdir(parents=True)
    (day / "call.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"wall": 100.0, "ev": "call.created", "engine": "grok"},
        {"wall": 110.0, "ev": "delegation.start"}, {"wall": 112.0, "ev": "guest.said"},
        {"wall": 130.0, "ev": "call.closed", "reason": "mobile_closed"}]))
    out = json.loads(z.make_status(cfg_for(tmp_path))({}))
    assert (out["engine"], out["guest"]) == ("grok", "grok")
    assert out["lane_server"].startswith("down")
    assert out["latest_call"]["seconds"] == 30.0
    assert out["latest_call"]["events"] == {"delegation.start": 1, "guest.said": 1}


def test_configure_rejects_bad_input_without_writing(tmp_path):
    configure = z.make_configure(cfg_for(tmp_path))
    assert "error" in json.loads(configure({"engine": "siri"}))
    assert "error" in json.loads(configure({"guest": "siri", "tiers": None}))
    assert "error" in json.loads(configure({"guest": "grok", "tiers": {"fast": [{"model": "x"}]}}))
    assert not (tmp_path / "tiers.json").exists() and not (tmp_path / "voices.json").exists()


def test_configure_writes_guest_and_tiers_then_resets(tmp_path):
    configure = z.make_configure(cfg_for(tmp_path))
    tiers = {"fast": [{"provider": "local", "model": "gemma4-e4b"}]}
    assert json.loads(configure({"guest": "grok", "tiers": tiers}))["ok"]
    assert json.loads((tmp_path / "voices.json").read_text()) == {"guest": "grok"}
    assert json.loads((tmp_path / "tiers.json").read_text()) == tiers
    assert json.loads(configure({"guest": "off", "tiers": None}))["applied"] == {"guest": "off", "tiers": "defaults"}
    assert json.loads((tmp_path / "voices.json").read_text()) == {"guest": ""}
    assert not (tmp_path / "tiers.json").exists()


def test_register_declares_what_the_manifest_lists():
    tools, commands = [], []

    class Ctx:
        def get_config(self, key, default=None):
            return default

        def register_tool(self, name, **kw):
            tools.append(name)

        def register_command(self, name, **kw):
            commands.append(name)

    z.register(Ctx())
    manifest = (ROOT / "plugin.yaml").read_text()
    assert all(name in manifest for name in tools) and len(tools) == 3
    assert commands == ["z0live-lanes"]
