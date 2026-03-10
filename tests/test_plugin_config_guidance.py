from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.engine import PluginRegistry
from core.webui import _collect_plugins_payload


class _FakePlugin:
    name = "demo_plugin"
    description = "demo"
    config_schema = {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
            "timeout_seconds": {"type": "integer"},
        },
    }


class PluginConfigGuidanceTests(unittest.TestCase):
    def test_build_plugin_meta_prefers_local_file_for_interactive_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = PluginRegistry(root / "plugins", logging.getLogger("test-plugin-meta"), config_dir=root / "config")
            registry._plugin_config_dir = root / "plugins" / "config"

            meta = registry._build_plugin_meta(
                name="connect_cli",
                plugin=_FakePlugin(),
                config={},
                needs_setup=True,
                supports_interactive_setup=True,
            )

        self.assertTrue(meta["configurable"])
        self.assertEqual(meta["config_target"], "plugins/config/connect_cli.yml")
        self.assertEqual(meta["setup_mode"], "wizard")
        self.assertIn("enabled", meta["editable_keys"])

    def test_collect_plugins_payload_exposes_guidance_fields(self) -> None:
        plugin = _FakePlugin()
        engine = SimpleNamespace(
            config={},
            plugins=SimpleNamespace(
                plugins={"demo_plugin": plugin},
                schemas=[],
                _plugin_configs={"demo_plugin": {"enabled": True}},
                _plugin_meta={
                    "demo_plugin": {
                        "configurable": True,
                        "config_target": "config/plugins.yml -> demo_plugin",
                        "config_guide": ["配置入口: config/plugins.yml -> demo_plugin"],
                        "editable_keys": ["enabled", "timeout_seconds"],
                        "supports_interactive_setup": False,
                        "needs_setup": False,
                        "using_defaults": False,
                    }
                },
                _unified_plugin_config={},
                _plugin_config_dir=Path("plugins/config"),
                _config_dir=Path("config"),
            ),
        )

        payload = _collect_plugins_payload(engine)

        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["config_target"], "config/plugins.yml -> demo_plugin")
        self.assertEqual(payload[0]["config_guide"], ["配置入口: config/plugins.yml -> demo_plugin"])
        self.assertEqual(payload[0]["editable_keys"], ["enabled", "timeout_seconds"])
        self.assertTrue(payload[0]["configurable"])


if __name__ == "__main__":
    unittest.main()
