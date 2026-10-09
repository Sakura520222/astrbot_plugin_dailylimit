"""Regression tests for loaded limit state and enabled time-period selection."""

import asyncio
import unittest
from unittest.mock import patch

from support import Event, FixedDateTime, make_plugin, request, series, web_save


class ConfigReloadTests(unittest.TestCase):
    def setUp(self):
        self.plugin = make_plugin()

    def reload(self, **updates):
        self.plugin.config["limits"].update(updates)
        self.plugin._load_limits_from_config()

    def assert_rules(self, groups, users, modes):
        self.assertEqual(self.plugin.group_limits, groups)
        self.assertEqual(self.plugin.user_limits, users)
        self.assertEqual(self.plugin.group_modes, modes)

    def test_startup_loads_rules_from_config(self):
        self.assert_rules({"100": 2, "200": 3}, {"11": 4, "12": 3},
                          {"200": "individual"})
        self.assertEqual(self.plugin.limiter.get_user_limit("20", "100"), 2)
        self.assertEqual(self.plugin.limiter.get_user_limit("11", "100"), 4)
        self.assertEqual(self.plugin.limiter.get_group_mode("200"), "individual")

    def test_reload_deletes_absent_rules(self):
        self.reload(group_limits="100:2", user_limits="11:4", group_mode_settings="")
        self.assert_rules({"100": 2}, {"11": 4}, {})
        self.assertEqual(self.plugin.limiter.get_user_limit("12", "200"), 5)
        self.assertEqual(self.plugin.limiter.get_group_mode("200"), "shared")

    def test_reload_replaces_entities_and_values(self):
        self.reload(group_limits="300:7", user_limits="44:8",
                    group_mode_settings="300:individual")
        self.assert_rules({"300": 7}, {"44": 8}, {"300": "individual"})
        self.assertEqual(self.plugin.limiter.get_user_limit("20", "100"), 5)
        self.assertEqual(self.plugin.limiter.get_user_limit("44", "300"), 8)

    def test_reload_changes_existing_values(self):
        self.reload(group_limits="100:9", user_limits="11:6",
                    group_mode_settings="200:shared")
        self.assert_rules({"100": 9}, {"11": 6}, {"200": "shared"})

    def test_reload_clears_all_rules(self):
        original_maps = (self.plugin.group_limits, self.plugin.user_limits,
                         self.plugin.group_modes)
        self.reload(group_limits="", user_limits="", group_mode_settings="")
        current_maps = (self.plugin.group_limits, self.plugin.user_limits,
                        self.plugin.group_modes)
        for original, current in zip(original_maps, current_maps, strict=True):
            self.assertIs(current, original)
        self.assert_rules({}, {}, {})
        self.assertEqual(self.plugin.limiter.get_user_limit("11", "100"), 5)

    def test_invalid_replacements_do_not_keep_previous_rules(self):
        self.reload(group_limits="100:invalid", user_limits="11:invalid",
                    group_mode_settings="200:unsupported")
        self.assert_rules({}, {}, {})

    def test_web_save_reloads_deleted_rules(self):
        web_save(self.plugin, {"group_limits": "", "user_limits": "",
                               "group_mode_settings": "", "skip_patterns": []})
        self.assertEqual(len(self.plugin.config.saved), 1)
        self.assert_rules({}, {}, {})
        self.assertEqual(self.plugin.limiter.get_user_limit("11", "100"), 5)
        self.assertFalse(self.plugin.limiter.should_skip_message("#hello"))


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.plugin = make_plugin()

    def test_private_user_limit_and_real_usage_records(self):
        self.assertEqual(series(self.plugin, 5, "11"), [True] * 4 + [False])
        self.assertEqual(sum(map(len, self.plugin.redis.lists.values())), 4)
        stats = self.plugin.redis.hashes["astrbot:usage_stats:2026-10-09:global"]
        self.assertEqual(stats["total_requests"], 4)
        self.assertFalse(any(name == "log_error" for name, args in self.plugin.logger.entries))

    def test_shared_group_limit_counts_both_users(self):
        outcomes = [request(self.plugin, "20", "100")[0],
                    request(self.plugin, "21", "100")[0],
                    request(self.plugin, "20", "100")[0]]
        self.assertEqual(outcomes, [True, True, False])
        self.assertEqual(self.plugin.redis.get(self.plugin._get_group_key("100")), 2)

    def test_individual_group_has_separate_user_counts(self):
        for user in ["20", "21"]:
            with self.subTest(user=user):
                self.assertEqual(series(self.plugin, 4, user, "200"), [True] * 3 + [False])
                self.assertEqual(self.plugin.redis.get(self.plugin._get_user_key(user, "200")), 3)

    def test_user_limit_overrides_individual_group_limit(self):
        self.assertEqual(series(self.plugin, 5, "11", "200"), [True] * 4 + [False])

    def test_priority_and_default_fallback(self):
        self.assertEqual(self.plugin.limiter.get_user_limit("12", "100"), 3)
        self.assertEqual(self.plugin.limiter.get_user_limit("13", "100"), 5)
        self.assertEqual(series(self.plugin, 4, "12", "100"), [True] * 3 + [False])
        self.assertEqual(series(make_plugin(), 6), [True] * 5 + [False])

    def test_exempt_user_is_not_limited_or_counted(self):
        self.assertEqual(series(self.plugin, 8, "99", "100"), [True] * 8)
        self.assertEqual(self.plugin.redis.strings, {})
        self.assertEqual(self.plugin.redis.lists, {})

    def test_skip_and_empty_prompt_stop_without_counting(self):
        for text, prompt in [("#ignored", "hello"), ("hello", " ")]:
            with self.subTest(text=text):
                allowed, event = request(self.plugin, text=text, prompt=prompt)
                self.assertFalse(allowed)
                self.assertTrue(event.stopped)
                self.assertEqual(self.plugin.redis.strings, {})


class TimePeriodTests(unittest.TestCase):
    def setUp(self):
        self.plugin = make_plugin()

    def add(self, limit, start="00:00", end="23:59"):
        asyncio.run(self.plugin.limit_timeperiod_add(Event("admin"), start, end, limit))

    def disable(self, index):
        asyncio.run(self.plugin.limit_timeperiod_disable(Event("admin"), index))

    def test_command_disable_stops_matching_and_time_period_counting(self):
        self.add(1)
        self.assertTrue(request(self.plugin)[0])
        period_key = self.plugin.limiter.get_time_period_usage_key("20")
        self.disable(1)
        command = Event("admin")
        asyncio.run(self.plugin.limit_timeperiod_list(command))
        self.assertIn("禁用", command.result)
        self.assertIn(":false", self.plugin.config.saved[-1]["limits"]["time_period_limits"])
        self.assertIsNone(self.plugin.limiter.get_current_time_period_limit())
        self.assertIsNone(self.plugin.time_period_mgr.get_current_time_period_limit())
        self.assertIsNone(self.plugin.limiter.get_time_period_usage_key("20"))
        self.assertIsNone(self.plugin.time_period_mgr.get_time_period_usage_key("20"))
        self.assertEqual(self.plugin.limiter.get_time_period_usage("20"), 0)
        self.assertFalse(self.plugin.limiter.increment_time_period_usage("20"))
        self.assertEqual(series(self.plugin, 6), [True] * 5 + [False])
        self.assertEqual(self.plugin.redis.get(period_key), 1)

    def test_disabled_overlap_selects_original_period_identity(self):
        self.add(1)
        self.add(4)
        self.add(2)
        self.disable(1)
        self.assertEqual(self.plugin.limiter.get_current_time_period_limit(), 4)
        self.assertEqual(self.plugin.time_period_mgr.get_current_time_period_limit(), 4)
        expected = self.plugin.limiter.get_time_period_usage_key("20", "100", 1)
        self.assertEqual(self.plugin.limiter.get_time_period_usage_key("20", "100"), expected)
        self.assertEqual(self.plugin.time_period_mgr.get_time_period_usage_key("20", "100"), expected)
        self.assertTrue(self.plugin.limiter.increment_time_period_usage("20", "100"))
        self.assertEqual(self.plugin.redis.get(expected), 1)

    def test_missing_enabled_flag_remains_enabled(self):
        self.add(3)
        del self.plugin.time_period_limits[0]["enabled"]
        self.assertEqual(self.plugin.limiter.get_current_time_period_limit(), 3)
        self.assertEqual(self.plugin.time_period_mgr.get_current_time_period_limit(), 3)
        self.assertIsNotNone(self.plugin.limiter.get_time_period_usage_key("20"))
        self.assertIsNotNone(self.plugin.time_period_mgr.get_time_period_usage_key("20"))

    def test_limiter_cross_midnight_selection_is_preserved(self):
        self.add(4, "22:00", "06:00")
        with patch.object(FixedDateTime, "now", return_value=FixedDateTime(2026, 10, 9, 1)):
            self.assertEqual(self.plugin.limiter.get_current_time_period_limit(), 4)
            expected = self.plugin.limiter.get_time_period_usage_key("20", time_period_id=0)
            self.assertEqual(self.plugin.limiter.get_time_period_usage_key("20"), expected)
            self.assertIn(":2026-10-09:", expected)

    def test_explicit_period_id_remains_compatible(self):
        self.add(1)
        self.disable(1)
        expected = "astrbot:time_period_limit:2026-10-09:7:100:20"
        self.assertEqual(self.plugin.limiter.get_time_period_usage_key("20", "100", 7), expected)
        self.assertEqual(self.plugin.time_period_mgr.get_time_period_usage_key("20", "100", 7), expected)


if __name__ == "__main__":
    unittest.main()
