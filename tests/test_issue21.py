"""Issue #21: reset target isolation and exempt users' shared-group status."""

import asyncio
import copy
import unittest

from support import Event, FixedDateTime, make_plugin, request, series


class ResetRegressionTests(unittest.TestCase):
    def setUp(self):
        self.plugin = make_plugin()
        today = self.plugin._get_today_key()
        self.values = {
            self.plugin._get_group_key("100"): 2,
            self.plugin._get_user_key("20", "100"): 3,
            self.plugin._get_user_key("21", "100"): 4,
            self.plugin._get_group_key("200"): 5,
            self.plugin._get_user_key("20", "200"): 6,
            self.plugin._get_user_key("100", "200"): 7,
            self.plugin._get_user_key("20"): 8,
            self.plugin._get_user_key("100"): 9,
            self.plugin._get_user_key("20", "1000"): 10,
            today.replace("2026-10-09", "2026-10-08") + ":group:100": 11,
            "another_plugin:100": 12,
        }
        for key, value in self.values.items():
            self.plugin.redis.set(key, value, ex=3600)
        self.plugin.redis.rpush("astrbot:usage_record:2026-10-09:100:20", "record")
        self.plugin.redis.hincrby("astrbot:usage_stats:2026-10-09:global", "total_requests", 3)

    def reset(self, *args):
        event = Event("admin")
        asyncio.run(self.plugin.limit_reset(event, *args))
        return event.result

    def assert_deleted_only(self, deleted):
        for key, value in self.values.items():
            with self.subTest(key=key):
                self.assertEqual(self.plugin.redis.get(key), None if key in deleted else value)
                self.assertEqual(self.plugin.redis.ttl(key), -2 if key in deleted else 3600)
        self.assertEqual(self.plugin.redis.lrange("astrbot:usage_record:2026-10-09:100:20", 0, -1),
                         ["record"])
        self.assertEqual(self.plugin.redis.hget("astrbot:usage_stats:2026-10-09:global",
                                              "total_requests"), 3)

    def test_group_reset_deletes_shared_and_individual_counters_only(self):
        result = self.reset("group", "100")
        self.assertIn("已重置群组 100", result)
        self.assertIn("群组: 1, 用户: 2", result)
        self.assert_deleted_only({self.plugin._get_group_key("100"),
                                  self.plugin._get_user_key("20", "100"),
                                  self.plugin._get_user_key("21", "100")})

    def test_user_reset_preserves_shared_group_with_same_numeric_id(self):
        self.assertIn("已重置用户 100", self.reset("100"))
        self.assert_deleted_only({self.plugin._get_user_key("100", "200"),
                                  self.plugin._get_user_key("100")})

    def test_all_reset_only_clears_current_daily_counters(self):
        self.assertIn("已重置所有使用记录", self.reset("ALL"))
        prefix = self.plugin._get_today_key() + ":"
        self.assert_deleted_only({key for key in self.values if key.startswith(prefix)})

    def test_invalid_and_missing_targets_do_not_delete_any_data(self):
        for args, message in [((), "重置使用次数命令用法"),
                              (("group",), "用法: /limit reset group <群组ID>"),
                              (("group", "abc"), "群组ID格式错误"),
                              (("group", "-100"), "群组ID格式错误"),
                              (("abc",), "用户ID格式错误"),
                              (("all", "100"), "用法:"),
                              (("20", "100"), "用法:")]:
            with self.subTest(args=args):
                self.assertIn(message, self.reset(*args))
                self.assert_deleted_only(set())

    def test_unknown_group_and_repeated_reset_are_safe(self):
        self.assertIn("未找到群组 999", self.reset("group", "999"))
        self.assert_deleted_only(set())
        self.reset("group", "100")
        self.assertIn("未找到群组 100", self.reset("group", "100"))

    def test_reset_restores_shared_and_individual_group_requests(self):
        for group, users in [("100", ["20", "21"]), ("200", ["20"])]:
            with self.subTest(group=group):
                self.plugin = make_plugin()
                limit = self.plugin.group_limits[group]
                self.assertEqual(series(self.plugin, limit + 1, users[0], group),
                                 [True] * limit + [False])
                self.reset("group", group)
                for user in users:
                    self.assertTrue(request(self.plugin, user, group)[0])

    def test_custom_reset_time_selects_current_reset_bucket(self):
        self.plugin = make_plugin({"daily_reset_time": "06:00"},
                                  at=FixedDateTime(2026, 10, 9, 5, 59))
        current = self.plugin._get_group_key("100")
        future = current.replace("2026-10-08", "2026-10-09")
        self.plugin.redis.set(current, 2, ex=60)
        self.plugin.redis.set(future, 9, ex=86400)
        self.assertIn("已重置群组 100", self.reset("group", "100"))
        self.assertIsNone(self.plugin.redis.get(current))
        self.assertEqual(self.plugin.redis.get(future), 9)
        self.assertEqual(self.plugin.redis.ttl(future), 86400)

    def test_disconnected_redis_does_not_report_success(self):
        self.plugin.redis = None
        self.assertIn("Redis未连接", self.reset("group", "100"))


class ExemptStatusRegressionTests(unittest.TestCase):
    def setUp(self):
        self.plugin = make_plugin()

    def status(self, user="99", group="100"):
        event = Event(user, group)
        asyncio.run(self.plugin.limit_status(event))
        return event.result

    def state(self):
        redis = self.plugin.redis
        return copy.deepcopy((redis.strings, redis.lists, redis.hashes, redis.expiries,
                              dict(self.plugin.config)))

    def test_exempt_user_sees_shared_usage_remaining_and_reset_without_mutation(self):
        self.assertTrue(request(self.plugin, "20", "100")[0])
        before = self.state()
        result = self.status()
        self.assertIn("豁免用户", result)
        self.assertIn("群组共享模式", result)
        self.assertIn("今日已使用：1/2 次", result)
        self.assertIn("剩余次数：1 次", result)
        self.assertIn("每日重置时间：00:00:00", result)
        self.assertNotIn("inf", result)
        self.assertEqual(self.state(), before)

    def test_exhausted_shared_budget_still_allows_uncounted_exempt_requests(self):
        self.assertEqual(series(self.plugin, 3, "20", "100"), [True, True, False])
        before = self.state()
        self.assertIn("剩余次数：0 次", self.status())
        self.assertEqual(series(self.plugin, 4, "99", "100"), [True] * 4)
        self.assertEqual(self.plugin._get_user_limit("99", "100"), float("inf"))
        self.assertEqual(self.state(), before)

    def test_group_budget_ignores_exempt_users_personal_and_priority_limits(self):
        self.plugin.user_limits["99"] = 50
        self.plugin.config["limits"]["priority_users"].append("99")
        self.assertIn("今日已使用：0/2 次", self.status())
        self.assertIn("今日已使用：0/5 次", self.status(group="300"))

    def test_active_time_period_uses_shared_counter_and_finite_period_limit(self):
        asyncio.run(self.plugin.limit_timeperiod_add(Event("admin"), "11:00", "13:00", 4))
        self.assertTrue(request(self.plugin, "20", "100")[0])
        before = self.state()
        result = self.status()
        self.assertIn("今日已使用：1/4 次", result)
        self.assertIn("剩余次数：3 次", result)
        self.assertIn("当前处于时间段限制：11:00-13:00", result)
        self.assertEqual(result.count("当前处于时间段限制"), 1)
        self.assertEqual(self.state(), before)

    def test_private_and_individual_exempt_status_stays_exempt_only(self):
        for group in [None, "200"]:
            with self.subTest(group=group):
                before = self.state()
                result = self.status(group=group)
                self.assertIn("豁免用户", result)
                self.assertNotIn("群组共享模式", result)
                self.assertNotIn("今日已使用", result)
                self.assertEqual(self.state(), before)

    def test_non_exempt_private_individual_and_shared_status_is_preserved(self):
        for group, limit, mode in [(None, 5, "个人"), ("200", 3, "独立"),
                                   ("100", 2, "共享")]:
            with self.subTest(group=group):
                self.assertTrue(request(self.plugin, "20", group)[0])
                result = self.status("20", group)
                self.assertIn(mode, result)
                self.assertIn(f"今日已使用：1/{limit} 次", result)
                self.assertNotIn("豁免用户", result)

    def test_query_permission_applies_to_exempt_users_too(self):
        self.plugin.config["limits"]["allow_normal_users_check_limit"] = False
        self.plugin.admin_users = ["admin"]
        before = self.state()
        self.assertEqual(self.status(), "您没有权限查询使用限制")
        self.assertIn("群组共享模式", self.status("admin"))
        self.plugin.admin_users.append("99")
        self.assertIn("今日已使用：0/2 次", self.status())
        self.assertEqual(self.state(), before)

    def test_custom_messages_and_visibility_settings_are_honored(self):
        self.plugin.config["limits"].update({"show_progress_bar": False,
                                             "show_remaining_count": False})
        self.plugin.config["limits"]["custom_messages"].update({
            "limit_status_exempt_message": "个人仍然豁免",
            "limit_status_group_shared_message": "群共享 {usage}/{limit}，余 {remaining}"})
        self.assertEqual(self.status(), "个人仍然豁免\n\n群共享 0/2，余 2")
        del self.plugin.config["limits"]["custom_messages"]["limit_status_group_shared_message"]
        result = self.status()
        self.assertIn("今日已使用：0/2 次", result)
        self.assertNotIn("📈", result)
        self.assertNotIn("剩余次数", result)


if __name__ == "__main__":
    unittest.main()
