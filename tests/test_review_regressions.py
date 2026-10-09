"""Request-level regressions for quota identity, reset, status and reload safety."""

import asyncio
import copy
import json
import threading
import unittest
from unittest.mock import patch

from support import Event, FixedDateTime, make_plugin, request, series, web_save


class TimePeriodRequestTests(unittest.TestCase):
    def setUp(self):
        self.plugin = make_plugin()

    def at(self, day, hour, minute=0):
        FixedDateTime.current = FixedDateTime(2026, 10, day, hour, minute)

    def add(self, start, end, limit):
        asyncio.run(self.plugin.limit_timeperiod_add(Event("admin"), start, end, limit))

    def status(self, user="20"):
        event = Event(user)
        asyncio.run(self.plugin.limit_status(event))
        return event.result

    def assert_request_period(self, expected_usage, expected_limit):
        self.assertEqual(self.plugin._get_usage_info("20", None)[:2],
                         (expected_usage, expected_limit))
        self.assertEqual(self.plugin.limiter.get_current_time_period_limit(), expected_limit)
        self.assertEqual(self.plugin.time_period_mgr.get_current_time_period_limit(), expected_limit)
        key = self.plugin.limiter.get_time_period_usage_key("20")
        self.assertEqual(self.plugin.time_period_mgr.get_time_period_usage_key("20"), key)
        return key

    def assert_records(self, date, count, expected_ttl):
        record_key = f"astrbot:usage_record:{date}:private_chat:20"
        records = self.plugin.redis.lists.get(record_key, [])
        self.assertEqual(len(records), count)
        self.assertTrue(all(json.loads(item)["date"] == date for item in records))
        self.assertEqual(self.plugin.redis.ttl(record_key), expected_ttl)
        stats_key = f"astrbot:usage_stats:{date}:global"
        self.assertEqual(self.plugin.redis.hget(stats_key, "total_requests"), count)
        self.assertEqual(self.plugin.redis.ttl(stats_key), expected_ttl)

    def test_cross_midnight_uses_same_request_counter_until_custom_reset(self):
        self.plugin = make_plugin({"daily_reset_time": "06:00"},
                                  at=FixedDateTime(2026, 10, 8, 23, 58))
        self.add("22:00", "06:00", 2)
        self.assertTrue(request(self.plugin)[0])
        key_before = self.assert_request_period(1, 2)
        self.assertIn(":2026-10-08:", key_before)
        self.assertEqual(self.plugin.redis.ttl(key_before), 21720)
        self.at(9, 0, 1)
        key_after = self.assert_request_period(1, 2)
        self.assertEqual(key_after, key_before)
        self.assertEqual(self.plugin.redis.ttl(key_after), 21540)
        self.assertTrue(request(self.plugin)[0])
        self.assertFalse(request(self.plugin)[0])
        self.assert_request_period(2, 2)
        self.assert_records("2026-10-08", 2, 21540)

    def test_custom_reset_splits_non_cross_midnight_period_keys_and_records(self):
        self.plugin = make_plugin({"daily_reset_time": "06:00"},
                                  at=FixedDateTime(2026, 10, 9, 5, 59))
        self.add("01:00", "12:00", 1)
        key_before = self.assert_request_period(0, 1)
        self.assertIn(":2026-10-08:", key_before)
        self.assertTrue(request(self.plugin)[0])
        self.assertFalse(request(self.plugin)[0])
        self.assertEqual(self.plugin.redis.ttl(key_before), 60)
        self.assert_records("2026-10-08", 1, 60)
        self.at(9, 6)
        key_after = self.assert_request_period(0, 1)
        self.assertIn(":2026-10-09:", key_after)
        self.assertNotEqual(key_before, key_after)
        self.assertIsNone(self.plugin.redis.get(key_before))
        self.assertTrue(request(self.plugin)[0])
        self.assertFalse(request(self.plugin)[0])
        self.assertEqual(self.plugin.redis.ttl(key_after), 86400)
        self.assert_records("2026-10-09", 1, 86400)

    def test_status_selects_enabled_cross_midnight_overlap(self):
        self.at(9, 1)
        self.add("00:00", "23:59", 1)
        self.add("22:00", "06:00", 4)
        asyncio.run(self.plugin.limit_timeperiod_disable(Event("admin"), 1))
        self.assertTrue(request(self.plugin)[0])
        text = self.status()
        self.assertIn("当前处于时间段限制：22:00-06:00", text)
        self.assertNotIn("当前处于时间段限制：00:00-23:59", text)
        self.assertIn("时间段内已使用：1/4", text)
        exempt_text = self.status("99")
        self.assertIn("当前处于时间段限制：22:00-06:00", exempt_text)
        self.assertIn("时间段限制：4 次", exempt_text)

    def test_status_omits_disabled_only_period(self):
        self.add("00:00", "23:59", 1)
        asyncio.run(self.plugin.limit_timeperiod_disable(Event("admin"), 1))
        self.assertTrue(request(self.plugin)[0])
        self.assertNotIn("当前处于时间段限制", self.status())

    def test_status_skips_disabled_daytime_overlap_for_normal_and_exempt_users(self):
        self.add("00:00", "23:59", 1)
        self.add("11:00", "13:00", 4)
        asyncio.run(self.plugin.limit_timeperiod_disable(Event("admin"), 1))
        self.assertTrue(request(self.plugin)[0])
        for user in ["20", "99"]:
            with self.subTest(user=user):
                text = self.status(user)
                self.assertIn("当前处于时间段限制：11:00-13:00", text)
                self.assertNotIn("当前处于时间段限制：00:00-23:59", text)
                self.assertIn("时间段限制：4 次", text)
        self.assertIn("时间段内已使用：1/4", self.status())

    def test_remove_migrates_legacy_counts_without_reassigning_survivor(self):
        self.add("08:00", "10:00", 3)
        self.add("11:00", "13:00", 4)
        # These are persisted keys from an older installation, not new counters.
        prefix = "astrbot:time_period_limit:2026-10-09:"
        self.plugin.redis.set(prefix + "0:private_chat:20", 2, ex=3600)
        self.plugin.redis.set(prefix + "1:private_chat:20", 3, ex=7200)
        key_before = self.plugin.limiter.get_time_period_usage_key("20")
        asyncio.run(self.plugin.limit_timeperiod_remove(Event("admin"), 1))
        key_after = self.plugin.limiter.get_time_period_usage_key("20")
        self.assertEqual(key_after, key_before)
        self.assert_request_period(3, 4)
        self.assertEqual(self.plugin.redis.ttl(key_after), 43200)
        self.assertTrue(request(self.plugin)[0])
        self.assertFalse(request(self.plugin)[0])
        self.assertEqual(self.plugin.redis.get(key_after), 4)

    def test_web_reorder_and_limit_edit_preserve_each_legacy_quota(self):
        self.add("14:00", "16:00", 3)
        self.add("11:00", "13:00", 4)
        prefix = "astrbot:time_period_limit:2026-10-09:"
        self.plugin.redis.set(prefix + "0:private_chat:20", 2, ex=21600)
        self.plugin.redis.set(prefix + "1:private_chat:20", 3, ex=7200)
        key_b = self.plugin.limiter.get_time_period_usage_key("20")
        key_a = self.plugin.limiter.get_time_period_usage_key("20", time_period_id=0)
        web_save(self.plugin, {"time_period_limits": "11:00-13:00:5:true\n14:00-16:00:3:true"})
        self.assertEqual(self.plugin.time_period_limits[0]["start_time"], "11:00")
        self.assertEqual(self.plugin.limiter.get_time_period_usage_key("20"), key_b)
        self.assert_request_period(3, 5)
        self.assertEqual(series(self.plugin, 3), [True, True, False])
        self.at(9, 15)
        self.assertEqual(self.plugin.limiter.get_time_period_usage_key("20"), key_a)
        self.assert_request_period(2, 3)
        self.assertTrue(request(self.plugin)[0])
        self.assertFalse(request(self.plugin)[0])

    def test_legacy_and_existing_stable_counts_merge_once_until_configured_reset(self):
        self.add("00:00", "23:59", 6)
        self.assertTrue(request(self.plugin)[0])
        stable_key = self.plugin.limiter.get_time_period_usage_key("20")
        stable_ttl = self.plugin.redis.ttl(stable_key)
        legacy_key = "astrbot:time_period_limit:2026-10-09:0:private_chat:20"
        self.plugin.redis.set(legacy_key, 2, ex=86400)
        # A real command forces migration even after an earlier empty legacy scan.
        asyncio.run(self.plugin.limit_timeperiod_disable(Event("admin"), 1))
        asyncio.run(self.plugin.limit_timeperiod_enable(Event("admin"), 1))
        self.assertEqual(self.plugin.limiter.get_time_period_usage_key("20"), stable_key)
        self.assertIsNone(self.plugin.redis.get(legacy_key))
        self.assertEqual(self.plugin.redis.get(stable_key), 3)
        self.assertEqual(self.plugin.redis.ttl(stable_key), stable_ttl)
        self.plugin.time_period_mgr.before_time_period_change()
        self.assertEqual(self.plugin.redis.get(stable_key), 3)
        self.assertEqual(series(self.plugin, 4), [True, True, True, False])

    def test_exhausted_migrated_quota_survives_midnight_until_custom_reset(self):
        self.plugin = make_plugin({"daily_reset_time": "06:00",
                                   "time_period_limits": "00:00-23:59:1:true"},
                                  at=FixedDateTime(2026, 10, 9, 23))
        legacy_key = "astrbot:time_period_limit:2026-10-09:0:private_chat:20"
        self.plugin.redis.set(legacy_key, 1, ex=3600)
        self.plugin.time_period_mgr._migration_checked_redis = None
        allowed, event = request(self.plugin)
        self.assertFalse(allowed)
        self.assertTrue(event.stopped)
        stable_key = self.plugin.limiter.get_time_period_usage_key("20")
        self.assertIn(":2026-10-09:", stable_key)
        self.assertEqual(self.plugin.redis.get(stable_key), 1)
        original_ttl = self.plugin.redis.ttl(stable_key)
        self.assertIsNone(self.plugin.redis.get(legacy_key))
        # No accepted request renews expiry before the reset boundary.
        for hour, minute, expected_ttl in [(0, 0, 21600), (5, 59, 60)]:
            self.at(10, hour, minute)
            allowed, event = request(self.plugin)
            self.assertFalse(allowed, "exhausted quota reopened before configured reset")
            self.assertTrue(event.stopped)
            self.assertEqual(self.plugin.limiter.get_time_period_usage_key("20"), stable_key)
            self.assertEqual(self.plugin.redis.get(stable_key), 1)
            self.assertEqual(self.plugin.redis.ttl(stable_key), expected_ttl)
            self.assertEqual(self.plugin.redis.lists, {})
        self.assertEqual(original_ttl, 25200)
        self.at(10, 6)
        self.assertIsNone(self.plugin.redis.get(stable_key))
        self.assertTrue(request(self.plugin)[0])
        new_key = self.assert_request_period(1, 1)
        self.assertIn(":2026-10-10:", new_key)
        self.assertEqual(self.plugin.redis.ttl(new_key), 86400)
        self.assert_records("2026-10-10", 1, 86400)

    def test_calendar_and_reset_legacy_buckets_share_next_reset_expiry(self):
        self.plugin = make_plugin({"daily_reset_time": "06:00",
                                   "time_period_limits": "00:00-23:59:3:true"},
                                  at=FixedDateTime(2026, 10, 9, 5, 59))
        calendar_key = "astrbot:time_period_limit:2026-10-09:0:private_chat:20"
        reset_key = "astrbot:time_period_limit:2026-10-08:0:private_chat:20"
        self.plugin.redis.set(calendar_key, 1, ex=86400)
        self.plugin.redis.set(reset_key, 2, ex=10)
        self.plugin.time_period_mgr._migration_checked_redis = None
        self.assertFalse(request(self.plugin)[0])
        stable_key = self.assert_request_period(3, 3)
        self.assertIn(":2026-10-08:", stable_key)
        self.assertEqual(self.plugin.redis.ttl(stable_key), 60)
        self.assertIsNone(self.plugin.redis.get(calendar_key))
        self.assertIsNone(self.plugin.redis.get(reset_key))
        self.at(9, 6)
        self.assertTrue(request(self.plugin)[0])
        new_key = self.assert_request_period(1, 3)
        self.assertIn(":2026-10-09:", new_key)
        self.assertEqual(self.plugin.redis.ttl(new_key), 86400)

    def test_existing_short_lived_stable_counter_is_repaired_before_request(self):
        self.plugin = make_plugin({"daily_reset_time": "06:00",
                                   "time_period_limits": "00:00-23:59:1:true"},
                                  at=FixedDateTime(2026, 10, 9, 23))
        identity = self.plugin.time_period_mgr.get_time_period_id(self.plugin.time_period_limits[0])
        stable_key = f"astrbot:time_period_limit:2026-10-09:{identity}:private_chat:20"
        historical_key = f"astrbot:time_period_limit:2026-10-08:{identity}:private_chat:20"
        self.plugin.redis.set(stable_key, 1, ex=3600)
        self.plugin.redis.set(historical_key, 9, ex=7200)
        self.plugin.time_period_mgr._migration_checked_redis = None
        self.assertFalse(request(self.plugin)[0])
        self.assertEqual(self.plugin.redis.get(stable_key), 1)
        self.assertEqual(self.plugin.redis.ttl(stable_key), 25200)
        self.assertEqual(self.plugin.redis.get(historical_key), 9)
        self.assertEqual(self.plugin.redis.ttl(historical_key), 7200)
        for hour, minute, expected_ttl in [(0, 0, 21600), (5, 59, 60)]:
            self.at(10, hour, minute)
            allowed, event = request(self.plugin)
            self.assertFalse(allowed)
            self.assertTrue(event.stopped)
            self.assertEqual(self.plugin.redis.get(stable_key), 1)
            self.assertEqual(self.plugin.redis.ttl(stable_key), expected_ttl)
            self.assertEqual(self.plugin.redis.lists, {})
        self.at(10, 6)
        self.assertTrue(request(self.plugin)[0])
        new_key = self.assert_request_period(1, 1)
        self.assertIn(":2026-10-10:", new_key)
        self.assertEqual(self.plugin.redis.ttl(new_key), 86400)
        self.assert_records("2026-10-10", 1, 86400)

    def test_historical_migration_preserves_original_expiry_policy(self):
        self.plugin = make_plugin({"daily_reset_time": "06:00",
                                   "time_period_limits": "00:00-23:59:3:true"},
                                  at=FixedDateTime(2026, 10, 9, 23))
        identity = self.plugin.time_period_mgr.get_time_period_id(self.plugin.time_period_limits[0])
        for date, old_ttl, new_ttl, expected_ttl in [
            ("2026-10-05", 120, None, 120),
            ("2026-10-06", 120, 240, 240),
            ("2026-10-07", None, 240, -1),
        ]:
            with self.subTest(date=date):
                legacy = f"astrbot:time_period_limit:{date}:0:private_chat:20"
                target = f"astrbot:time_period_limit:{date}:{identity}:private_chat:20"
                self.plugin.redis.set(legacy, 2, ex=old_ttl)
                if new_ttl is not None:
                    self.plugin.redis.set(target, 1, ex=new_ttl)
                self.plugin.time_period_mgr.before_time_period_change()
                self.assertIsNone(self.plugin.redis.get(legacy))
                self.assertEqual(self.plugin.redis.get(target), 2 if new_ttl is None else 3)
                self.assertEqual(self.plugin.redis.ttl(target), expected_ttl)

    def test_unknown_legacy_index_stops_requests_without_changing_counters(self):
        self.plugin = make_plugin({"time_period_limits": "11:00-13:00:1:true"})
        legacy_key = "astrbot:time_period_limit:2026-10-09:8:private_chat:20"
        self.plugin.redis.set(legacy_key, 3, ex=3600)
        self.plugin.time_period_mgr._migration_checked_redis = None
        before = dict(self.plugin.redis.strings)
        for attempt in range(4):
            with self.subTest(attempt=attempt):
                allowed, event = request(self.plugin)
                self.assertFalse(allowed)
                self.assertTrue(event.stopped)
                self.assertEqual(self.plugin.redis.strings, before)
                self.assertEqual(self.plugin.redis.lists, {})


class ConcurrentReloadTests(unittest.TestCase):
    def test_paused_parse_keeps_complete_old_rules_and_publishes_in_place(self):
        plugin = make_plugin()
        loader = plugin.config_loader
        aliases = (plugin.group_limits, plugin.user_limits, plugin.group_modes)
        old_maps = ({"100": 2, "200": 3}, {"11": 4, "12": 3}, {"200": "individual"})
        new_maps = ({"200": 8}, {"11": 9}, {"200": "shared"})
        parsing = threading.Event()
        resume = threading.Event()
        reader_done = threading.Event()
        errors = []
        observations = []
        original_parse = type(loader).parse_limits_config
        original_usage = plugin._get_usage_info

        def paused_parse(instance, config_key, limits_dict, limit_type):
            if config_key == "group_limits":
                parsing.set()
                if not resume.wait(3):
                    raise TimeoutError("test did not release config parsing")
            return original_parse(instance, config_key, limits_dict, limit_type)

        def capture_usage(user, group):
            value = original_usage(user, group)
            observations.append(value)
            return value

        def reload_config():
            try:
                plugin._load_limits_from_config()
            except Exception as error:  # noqa: BLE001 -- report thread failures to the test
                errors.append(error)

        def read_request():
            try:
                request(plugin, "11", "200")
            except Exception as error:  # noqa: BLE001 -- report thread failures to the test
                errors.append(error)
            finally:
                reader_done.set()

        plugin.config["limits"].update(group_limits="200:8", user_limits="11:9",
                                       group_mode_settings="200:shared")
        writer = threading.Thread(target=reload_config, daemon=True)
        reader = threading.Thread(target=read_request, daemon=True)
        with patch.object(type(loader), "parse_limits_config", paused_parse), \
                patch.object(plugin, "_get_usage_info", capture_usage):
            writer.start()
            try:
                self.assertTrue(parsing.wait(3), "reload did not reach parser")
                self.assertEqual(tuple(map(dict, aliases)), old_maps)
                reader.start()
                # A reader may see old state immediately or wait for publication.
                if reader_done.wait(0.2):
                    self.assertEqual(observations[0][1:], (4, "个人独立"))
            finally:
                resume.set()
                writer.join(3)
                if reader.ident is not None:
                    reader.join(3)
            self.assertFalse(writer.is_alive(), "reload thread did not finish")
            self.assertFalse(reader.is_alive(), "request thread did not finish")
        self.assertEqual(errors, [])
        self.assertEqual(len(observations), 1)
        self.assertIn(observations[0][1:], [(4, "个人独立"), (9, "群组共享")])
        self.assertEqual(tuple(map(dict, aliases)), new_maps)
        for alias, current in zip(aliases, (plugin.group_limits, plugin.user_limits,
                                           plugin.group_modes), strict=True):
            self.assertIs(alias, current)

    def test_setmode_waits_for_reload_publication_before_mutating_config(self):
        plugin = make_plugin()
        loader = plugin.config_loader
        alias = plugin.group_modes
        parsing = threading.Event()
        resume = threading.Event()
        command_started = threading.Event()
        command_done = threading.Event()
        errors = []
        original_parse = type(loader).parse_limits_config

        def paused_parse(instance, config_key, limits_dict, limit_type):
            if config_key == "group_limits":
                parsing.set()
                if not resume.wait(3):
                    raise TimeoutError("test did not release config parsing")
            return original_parse(instance, config_key, limits_dict, limit_type)

        def reload_config():
            try:
                plugin._load_limits_from_config()
            except Exception as error:  # noqa: BLE001 -- report thread failures to the test
                errors.append(error)

        def setmode():
            command_started.set()
            try:
                asyncio.run(plugin.limit_setmode(Event("admin", "200"), "individual"))
            except Exception as error:  # noqa: BLE001 -- report thread failures to the test
                errors.append(error)
            finally:
                command_done.set()

        plugin.config["limits"]["group_mode_settings"] = "200:shared"
        writer = threading.Thread(target=reload_config, daemon=True)
        command = threading.Thread(target=setmode, daemon=True)
        with patch.object(type(loader), "parse_limits_config", paused_parse):
            writer.start()
            try:
                self.assertTrue(parsing.wait(3), "reload did not reach parser")
                command.start()
                self.assertTrue(command_started.wait(3))
                self.assertFalse(command_done.wait(0.2), "setmode bypassed the reload lock")
            finally:
                resume.set()
                writer.join(3)
                if command.ident is not None:
                    command.join(3)
            self.assertFalse(writer.is_alive(), "reload thread did not finish")
            self.assertFalse(command.is_alive(), "command thread did not finish")
        self.assertEqual(errors, [])
        self.assertIs(plugin.group_modes, alias)
        self.assertEqual(alias, {"200": "individual"})
        self.assertEqual(plugin.config["limits"]["group_mode_settings"], "200:individual")


class WebUpdateFailureTests(unittest.TestCase):
    def setUp(self):
        self.plugin = make_plugin({"time_period_limits": "11:00-13:00:1:true"})
        self.plugin.config.save_config()
        self.previous_config = copy.deepcopy(dict(self.plugin.config))
        self.aliases = (self.plugin.group_limits, self.plugin.user_limits,
                        self.plugin.group_modes, self.plugin.time_period_limits)
        self.previous_rules = copy.deepcopy(self.aliases)

    def assert_previous_state(self):
        self.assertEqual(dict(self.plugin.config), self.previous_config)
        self.assertEqual(self.plugin.config.saved[-1], self.previous_config)
        current = (self.plugin.group_limits, self.plugin.user_limits,
                   self.plugin.group_modes, self.plugin.time_period_limits)
        for alias, published, previous in zip(self.aliases, current, self.previous_rules,
                                               strict=True):
            self.assertIs(published, alias)
            self.assertEqual(published, previous)

    def test_web_preflights_unknown_legacy_before_any_limit_change(self):
        legacy_key = "astrbot:time_period_limit:2026-10-09:8:private_chat:20"
        self.plugin.redis.set(legacy_key, 3, ex=3600)
        before = dict(self.plugin.redis.strings)
        with self.assertRaises(RuntimeError):
            web_save(self.plugin, {"user_limits": "11:99", "default_daily_limit": 100})
        self.assert_previous_state()
        self.assertEqual(self.plugin.redis.strings, before)

    def test_web_save_failure_restores_and_saves_previous_state(self):
        save = self.plugin.config.save_config
        attempts = []

        def fail_once():
            attempts.append(True)
            if len(attempts) == 1:
                raise OSError("simulated configuration save failure")
            return save()

        with patch.object(self.plugin.config, "save_config", fail_once), \
                self.assertRaises(OSError):
            web_save(self.plugin, {"user_limits": "11:99", "default_daily_limit": 100})
        self.assertEqual(len(attempts), 2)
        self.assert_previous_state()

    def test_runtime_reload_without_redis_keeps_original_periods(self):
        self.plugin.redis_client.redis = None
        self.plugin.redis = None
        self.plugin.config["limits"]["time_period_limits"] = "10:00-14:00:2:true"
        with self.assertRaises(RuntimeError):
            self.plugin._load_limits_from_config()
        self.assertIs(self.plugin.time_period_limits, self.aliases[3])
        self.assertEqual(self.plugin.time_period_limits, self.previous_rules[3])


if __name__ == "__main__":
    unittest.main()
