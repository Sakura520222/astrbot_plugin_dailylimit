"""Check release-facing configuration and command output without live services."""

import ast
import asyncio
import json
import types
import unittest

from support import ROOT, Event, make_plugin


class PluginContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(
            (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        )

    def test_passwords_are_masked_without_changing_defaults(self):
        for section, default in [("redis", ""), ("web_server", "limit")]:
            with self.subTest(section=section):
                field = self.schema[section]["items"]["password"]
                self.assertEqual(field["type"], "string")
                self.assertIs(field["secret"], True)
                self.assertEqual(field["default"], default)

    def test_schema_uses_astrbot_field_mapping_and_string_lists(self):
        pending = [self.schema]
        while pending:
            fields = pending.pop()
            for name, field in fields.items():
                with self.subTest(name=name):
                    self.assertIn("type", field)
                    if field["type"] == "object":
                        pending.append(field["items"])
                    elif field["type"] == "list":
                        self.assertIsInstance(field["default"], list)
                        self.assertNotIn("items", field)
        limits = self.schema["limits"]["items"]
        self.assertEqual(limits["skip_patterns"]["default"], ["#", "*"])
        self.assertEqual(limits["default_daily_limit"]["default"], 20)
        self.assertEqual(limits["daily_reset_time"]["default"], "00:00")

    def test_plugin_relies_on_metadata_instead_of_legacy_registration(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        plugin = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "DailyLimitPlugin"
        )
        self.assertEqual(plugin.decorator_list, [])

    def test_help_uses_installed_version_and_documents_group_reset(self):
        plugin = make_plugin()
        plugin.plugin_version = "v9.8.7"
        event = Event()
        asyncio.run(plugin.limit_help_all(event))
        self.assertEqual(event.result.count("v9.8.7"), 2)
        self.assertIn("/limit reset group <群组ID>", event.result)
        self.assertLess(
            event.result.index("1️⃣ 🏆 豁免用户"), event.result.index("2️⃣ ⏰ 时间段限制")
        )

    def test_update_message_points_to_maintenance_repository(self):
        plugin = make_plugin()
        plugin.config["version_check"] = {"enabled": True}

        async def check_version_update():
            return None

        plugin.version_checker = types.SimpleNamespace(
            check_version_update=check_version_update,
            last_checked_version="v9.8.7",
            last_checked_version_info={"content": "test release"},
            _compare_versions=lambda *_: 1,
        )
        event = Event()
        asyncio.run(plugin.limit_checkupdate(event))
        self.assertIn(
            "https://github.com/Sakura520222/astrbot_plugin_dailyLimit", event.result
        )
        self.assertNotIn("https://github.com/left666/", event.result)
