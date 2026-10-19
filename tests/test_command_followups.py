"""Issue #21 follow-ups: reset time, security state and trend identities."""

import asyncio
import copy
import unittest
from typing import ClassVar

from support import Event, FixedDateTime, make_plugin, request, series, web_save


def command(plugin, method, text):
    event = Event("admin", text=text)
    asyncio.run(getattr(plugin, method)(event))
    return event.result


class ResetTimeTests(unittest.TestCase):
    def setUp(self):
        self.plugin = make_plugin({"daily_reset_time": "06:00"})

    def get(self):
        return command(self.plugin, "limit_resettime", "/limit resettime get")

    def test_get_before_at_and_after_reset_uses_next_occurrence(self):
        for hour, minute, expected, remaining in [
            (5, 59, "2026-10-09 06:00:00", "0小时1分钟"),
            (6, 0, "2026-10-10 06:00:00", "24小时0分钟"),
            (12, 0, "2026-10-10 06:00:00", "18小时0分钟"),
        ]:
            with self.subTest(hour=hour, minute=minute):
                FixedDateTime.current = FixedDateTime(2026, 10, 9, hour, minute)
                before = copy.deepcopy(dict(self.plugin.config))
                text = self.get()
                self.assertIn("当前重置时间：06:00", text)
                self.assertIn("下次重置时间：" + expected, text)
                self.assertIn(remaining, text)
                self.assertEqual(dict(self.plugin.config), before)
                self.assertEqual(self.plugin.config.saved, [])

    def test_set_get_and_reset_succeed_and_persist(self):
        result = command(self.plugin, "limit_resettime", "/limit resettime set 07:30")
        self.assertIn("已设置每日重置时间为 07:30", result)
        self.assertIn("2026-10-10 07:30:00", result)
        self.assertEqual(self.plugin.config.saved[-1]["limits"]["daily_reset_time"], "07:30")
        self.assertIn("当前重置时间：07:30", self.get())
        self.assertIn("默认值 00:00", command(self.plugin, "limit_resettime", "/limit resettime reset"))
        self.assertIn("当前重置时间：00:00", self.get())
        self.assertIn("2026-10-10 00:00:00", self.get())

    def test_help_and_invalid_time_do_not_write_configuration(self):
        before = copy.deepcopy(dict(self.plugin.config))
        self.assertIn("使用方式", command(self.plugin, "limit_resettime", "/limit resettime"))
        self.assertIn("时间格式错误", command(self.plugin, "limit_resettime", "/limit resettime set 25:61"))
        self.assertEqual(dict(self.plugin.config), before)
        self.assertEqual(self.plugin.config.saved, [])


class SecurityStateTests(unittest.TestCase):
    SETTINGS: ClassVar[dict] = {"anti_abuse_enabled": True, "rapid_request_threshold": 3,
                "rapid_request_window": 2, "consecutive_request_threshold": 99,
                "consecutive_request_window": 5, "auto_block_duration": 37,
                "admin_notification_enabled": False}

    def setUp(self):
        self.plugin = make_plugin(security_updates=self.SETTINGS)

    def security(self, action):
        return command(self.plugin, "limit_security", "/limit security " + action)

    def assert_config_matches_runtime(self):
        for key in ["anti_abuse_enabled", "rapid_request_threshold", "rapid_request_window",
                    "consecutive_request_threshold", "consecutive_request_window", "auto_block_duration"]:
            with self.subTest(key=key):
                value = self.plugin.config["security"][key]
                self.assertEqual(getattr(self.plugin, key), value)
                self.assertEqual(getattr(self.plugin.security, key), value)

    def test_startup_status_config_and_detector_match_configured_values(self):
        self.assert_config_matches_runtime()
        before = copy.deepcopy(dict(self.plugin.config))
        for action in ["status", "config"]:
            with self.subTest(action=action):
                text = self.security(action)
                self.assertIn("防刷机制：✅ 已启用", text)
                self.assertIn("3次/2秒", text)
                self.assertIn("99次/5秒", text)
                self.assertIn("37秒", text)
        self.assertEqual(dict(self.plugin.config), before)
        self.assertEqual(self.plugin.config.saved, [])

    def test_detector_uses_configured_rapid_window_and_threshold(self):
        for timestamp in [1000, 1001, 1003, 1003.5]:
            self.assertFalse(self.plugin._detect_abuse_behavior("20", timestamp)["is_abuse"])
        result = self.plugin._detect_abuse_behavior("20", 1004)
        self.assertTrue(result["is_abuse"])
        self.assertEqual(result["type"], "rapid_request")
        self.assertEqual(result["count"], 3)

    def test_llm_request_blocks_at_configured_threshold_and_duration(self):
        self.plugin.config["security"].update({"rapid_request_threshold": 2,
                                                "rapid_request_window": 60})
        self.plugin._load_limits_from_config()
        self.assertTrue(request(self.plugin, "20", "100")[0])
        allowed, event = request(self.plugin, "20", "100")
        self.assertFalse(allowed)
        self.assertTrue(event.stopped)
        self.assertEqual(self.plugin.blocked_users["20"]["duration"], 37)
        self.assertEqual(int(self.plugin.redis.get(self.plugin._get_group_key("100"))), 1)

    def test_consecutive_detector_uses_configured_threshold_and_window(self):
        self.plugin.config["security"].update({"rapid_request_threshold": 99,
                                                "consecutive_request_threshold": 2})
        self.plugin._load_limits_from_config()
        self.assertFalse(self.plugin._detect_abuse_behavior("20", 100)["is_abuse"])
        self.assertFalse(self.plugin._detect_abuse_behavior("20", 106)["is_abuse"])
        result = self.plugin._detect_abuse_behavior("20", 110)
        self.assertTrue(result["is_abuse"])
        self.assertEqual(result["type"], "consecutive_request")

    def test_reload_and_command_toggles_share_the_same_state_and_record_aliases(self):
        aliases = (self.plugin.abuse_records, self.plugin.blocked_users, self.plugin.abuse_stats)
        self.plugin.config["security"].update({"anti_abuse_enabled": False,
                                                "rapid_request_threshold": 7,
                                                "auto_block_duration": 51})
        self.plugin._load_limits_from_config()
        self.assert_config_matches_runtime()
        self.assertIn("未启用", self.security("status"))
        self.assertIn("已启用", self.security("enable"))
        self.assert_config_matches_runtime()
        self.assertTrue(self.plugin.config.saved[-1]["security"]["anti_abuse_enabled"])
        self.assertIn("7次/2秒", self.security("config"))
        self.plugin._detect_abuse_behavior("20", 1000)
        asyncio.run(self.plugin._block_user_for_abuse("20", "test"))
        self.assertIn("20", self.plugin.blocked_users)
        self.assertIn("已禁用", self.security("disable"))
        self.assert_config_matches_runtime()
        self.assertFalse(self.plugin.config.saved[-1]["security"]["anti_abuse_enabled"])
        for original, current in zip(aliases, (self.plugin.abuse_records, self.plugin.blocked_users,
                                               self.plugin.abuse_stats), strict=True):
            self.assertIs(original, current)
            self.assertEqual(current, {})
        self.assertFalse(self.plugin.security.detect_abuse_behavior("20", 1001)["is_abuse"])

    def test_web_reload_updates_detector_and_status_together(self):
        self.plugin.config["security"]["rapid_request_threshold"] = 8
        web_save(self.plugin, {"group_limits": "100:2\n200:3"})
        self.assert_config_matches_runtime()
        self.assertIn("8次/2秒", self.security("status"))

    def test_exempt_requests_stay_exempt_when_detection_is_enabled(self):
        self.assertEqual(series(self.plugin, 6, "99", "100"), [True] * 6)
        self.assertEqual(self.plugin.abuse_records, {})
        self.assertEqual(self.plugin.redis.keys("*"), [])


class TrendIdentityTests(unittest.TestCase):
    def setUp(self):
        self.plugin = make_plugin()

    def trends(self, period="day"):
        event = Event("admin")
        asyncio.run(self.plugin.limit_trends(event, period))
        return event.result

    def test_real_requests_count_unique_people_and_groups_without_writes_on_query(self):
        self.assertTrue(request(self.plugin, "20", "100")[0])
        self.assertTrue(request(self.plugin, "21", "100")[0])
        self.assertTrue(request(self.plugin, "20", "200")[0])
        self.assertTrue(request(self.plugin, "20")[0])
        redis = self.plugin.redis
        before = copy.deepcopy((redis.strings, redis.lists, redis.hashes, redis.expiries))
        data = self.plugin.stats_analyzer.get_trend_data("daily")["2026-10-09"]
        self.assertEqual(data["total_requests"], 4)
        self.assertEqual(data["active_users"], 2)
        self.assertEqual(data["active_groups"], 2)
        text = self.trends()
        self.assertIn("总请求数趋势: 4 次", text)
        self.assertIn("活跃用户数: 2 人", text)
        self.assertIn("活跃群组数: 2 个", text)
        self.assertEqual((redis.strings, redis.lists, redis.hashes, redis.expiries), before)

    def test_legacy_hash_with_float_metadata_uses_existing_identity_fields(self):
        key = "astrbot:trend:daily:2026-10-09"
        for field, value in {"total_requests": 42, "user:20": 20, "user:21": 22,
                             "group:100": 22, "group:200": 20,
                             "usage_type:llm_request": 42, "updated_at": "1792400000.25"}.items():
            self.plugin.redis.hset(key, field, value)
        result = self.plugin.usage_tracker._get_trend_stats_by_key(key)
        self.assertEqual(result["total_requests"], 42)
        self.assertEqual(result["active_users"], 2)
        self.assertEqual(result["active_groups"], 2)
        self.assertEqual(result["usage_types"], {"llm_request": 42})

    def test_latest_period_drives_summary_instead_of_oldest_period(self):
        for day, count in [("2026-10-09", 42), ("2026-10-08", 75)]:
            key = "astrbot:trend:daily:" + day
            self.plugin.redis.hset(key, "total_requests", count)
            self.plugin.redis.hset(key, "user:20", count)
        text = self.trends()
        self.assertIn("总请求数趋势: 42 次", text)
        self.assertIn("活跃用户数: 1 人", text)

    def test_weekly_and_monthly_hashes_are_normalized_without_counting_metadata(self):
        for kind, date, period in [("weekly", "2026-W41", "week"), ("monthly", "2026-10", "month")]:
            with self.subTest(kind=kind):
                key = f"astrbot:trend:{kind}:{date}"
                for field, value in {"total_requests": 11, "user:20": 5, "user:21": 6,
                                     "group:100": 11}.items():
                    self.plugin.redis.hset(key, field, value)
                text = self.trends(period)
                self.assertIn("总请求数趋势: 11 次", text)
                self.assertIn("活跃用户数: 2 人", text)
                self.assertIn("活跃群组数: 1 个", text)

    def test_custom_reset_bucket_is_respected_for_daily_trends(self):
        self.plugin = make_plugin({"daily_reset_time": "06:00"},
                                  at=FixedDateTime(2026, 10, 9, 5, 59))
        self.assertTrue(request(self.plugin, "20", "100")[0])
        data = self.plugin.stats_analyzer.get_trend_data("daily")
        self.assertIn("2026-10-08", data)
        self.assertNotIn("2026-10-09", data)
        self.assertEqual(data["2026-10-08"]["active_users"], 1)

    def test_unpadded_week_numbers_are_sorted_chronologically(self):
        data = {"2026-W10": {"total_requests": 10, "active_users": 2, "active_groups": 1},
                "2026-W9": {"total_requests": 9, "active_users": 1, "active_groups": 1}}
        text = self.plugin.stats_analyzer.analyze_trends(data)
        self.assertIn("总请求数趋势: 10 次", text)
        self.assertIn("活跃用户数: 2 人", text)


if __name__ == "__main__":
    unittest.main()
