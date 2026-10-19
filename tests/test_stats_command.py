"""Regressions for /limit stats and the existing WebUI daily-quota totals."""

import ast
import asyncio
import copy
import unittest
from unittest.mock import patch

from support import ROOT, Event, FixedDateTime, make_plugin, request, series


def web_totals(plugin):
    """Run the real WebUI overview's quota aggregation, without Flask or disk IO."""
    path = ROOT / "web_server.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    original = next(node for node in tree.body
                    if isinstance(node, ast.ClassDef) and node.name == "WebServer")
    names = {"_update_active_users_stats", "_get_user_keys_for_date", "_calculate_total_requests"}
    body = [node for node in original.body if isinstance(node, ast.FunctionDef) and node.name in names]
    cls = ast.ClassDef(name="WebTotals", bases=[], keywords=[], body=body, decorator_list=[])
    namespace = {}
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    exec(compile(module, str(path), "exec"), namespace)  # noqa: S102 -- trusted repository source
    web = namespace["WebTotals"]()
    web.plugin = plugin
    result = {}
    web._update_active_users_stats(result, plugin._get_reset_period_date())
    return result


class StatsCommandTests(unittest.TestCase):
    def setUp(self):
        self.plugin = make_plugin()

    def stats(self):
        event = Event("admin")
        asyncio.run(self.plugin.limit_stats(event))
        return event.result

    def state(self):
        redis = self.plugin.redis
        return copy.deepcopy((redis.strings, redis.lists, redis.hashes, redis.expiries,
                              dict(self.plugin.config)))

    def assert_totals(self, users, calls):
        before = self.state()
        result = self.stats()
        self.assertIn(f"活跃用户数: {users}\n", result)
        self.assertIn(f"总调用次数: {calls}\n", result)
        self.assertIn("用户特定限制数: 2\n", result)
        self.assertIn("群组特定限制数: 2\n", result)
        self.assertIn("豁免用户数: 1", result)
        self.assertEqual(web_totals(self.plugin)["active_users"], users)
        self.assertEqual(web_totals(self.plugin)["total_requests"], calls)
        self.assertEqual(self.state(), before)
        self.assertFalse(any(name == "log_error" for name, _ in self.plugin.logger.entries))

    def test_empty_usage_still_shows_real_configuration_counts(self):
        self.assert_totals(0, 0)

    def test_reported_38_requests_and_five_quota_records_match_webui(self):
        self.plugin = make_plugin({"default_daily_limit": 100,
                                   "group_limits": "100:100\n200:100"})
        for group, user, count in [("100", "20", 10), ("300", "20", 10),
                                   ("400", "21", 10), ("200", "20", 4),
                                   (None, "21", 4)]:
            self.assertEqual(series(self.plugin, count, user, group), [True] * count)
        self.assert_totals(5, 38)

    def test_shared_users_are_not_double_counted_and_exempt_requests_stay_uncounted(self):
        self.assertTrue(request(self.plugin, "20", "100")[0])
        self.assertTrue(request(self.plugin, "21", "100")[0])
        self.assertTrue(request(self.plugin, "20", "200")[0])
        self.assertTrue(request(self.plugin, "20")[0])
        self.assertEqual(series(self.plugin, 3, "99", "100"), [True] * 3)
        # Preserve WebUI's existing definition: number of daily quota records,
        # including shared-group records, rather than unique human identities.
        self.assert_totals(3, 4)

    def test_reset_updates_current_quota_totals_without_using_historical_request_stats(self):
        self.assertTrue(request(self.plugin, "20", "100")[0])
        self.assertTrue(request(self.plugin, "20", "200")[0])
        self.assert_totals(2, 2)
        asyncio.run(self.plugin.limit_reset(Event("admin"), "group", "100"))
        self.assert_totals(1, 1)
        self.assertEqual(self.plugin.redis.hget("astrbot:usage_stats:2026-10-09:global",
                                              "total_requests"), 2)

    def test_custom_reset_time_selects_the_same_bucket_as_webui(self):
        self.plugin = make_plugin({"daily_reset_time": "06:00"},
                                  at=FixedDateTime(2026, 10, 9, 5, 59))
        key = self.plugin._get_group_key("100")
        self.plugin.redis.set(key, 2, ex=3600)
        self.plugin.redis.set(key.replace("2026-10-08", "2026-10-09"), 7, ex=3600)
        self.assert_totals(1, 2)
        FixedDateTime.current = FixedDateTime(2026, 10, 9, 6)
        self.assert_totals(1, 7)

    def test_unrelated_records_and_period_counters_do_not_pollute_daily_totals(self):
        self.plugin.redis.set(self.plugin._get_user_key("20"), 2, ex=3600)
        self.plugin.redis.set("astrbot:time_period_limit:2026-10-09:0:100:None", 9, ex=3600)
        self.plugin.redis.set("astrbot:daily_limit:2026-10-08:group:100", 8, ex=3600)
        self.plugin.redis.set("another_plugin:2026-10-09:100:20", 7, ex=3600)
        self.assert_totals(1, 2)

    def test_zero_valued_quota_record_matches_webui_count(self):
        self.plugin.redis.set(self.plugin._get_user_key("20"), 0, ex=3600)
        self.assert_totals(1, 0)

    def test_redis_query_error_is_reported_instead_of_successful_zero_totals(self):
        with patch.object(self.plugin.redis, "keys", side_effect=RuntimeError("redis unavailable")):
            result = self.stats()
        self.assertIn("获取统计信息失败", result)
        self.assertNotIn("今日统计信息", result)
        self.assertTrue(any(name == "log_error" and "redis unavailable" in str(args)
                            for name, args in self.plugin.logger.entries))

    def test_wrongtype_error_preserves_data_and_does_not_display_zero_totals(self):
        self.plugin.redis.rpush(self.plugin._get_user_key("20"), "invalid counter")
        before = self.state()
        self.assertIn("获取统计信息失败", self.stats())
        self.assertEqual(self.state(), before)

    def test_disconnected_or_stale_redis_connection_does_not_show_zero_totals(self):
        self.plugin.redis_client.redis = None
        self.assertIn("获取统计信息失败", self.stats())
        self.plugin.redis = None
        self.assertIn("Redis未连接", self.stats())


if __name__ == "__main__":
    unittest.main()
