"""Optional upstream AstrBot filter-source and real Redis regressions.

ASTRBOT_SOURCE selects a checked-out AstrBot source tree. We execute its unchanged
command and permission filter bodies, replacing only unrelated framework imports.
This exercises the real tokenizer, signature binding and permission decisions,
not plugin registration or a running AstrBot platform adapter.
DAILYLIMIT_TEST_REDIS_SERVER additionally selects a local Redis server executable.
"""

import ast
import asyncio
import os
import shutil
import socket
import subprocess
import tempfile
import time
import types
import unittest
from contextlib import ExitStack
from pathlib import Path

from support import ROOT, Event, make_plugin, request, series

ASTRBOT_SOURCE = os.environ.get("ASTRBOT_SOURCE")
REDIS_SERVER = os.environ.get("DAILYLIMIT_TEST_REDIS_SERVER")


def load_filters():
    """Load upstream algorithms with stdlib imports and light annotation stand-ins."""
    namespace = {"AstrBotConfig": dict, "AstrMessageEvent": Event,
                 "StarHandlerMetadata": types.SimpleNamespace,
                 "HandlerFilter": object, "CustomFilter": object}
    for filename in ["command.py", "permission.py"]:
        path = Path(ASTRBOT_SOURCE) / "astrbot/core/star/filter" / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        tree.body = [node for node in tree.body if not (
            isinstance(node, ast.ImportFrom)
            and (node.level or (node.module or "").startswith("astrbot")))]
        exec(compile(tree, str(path), "exec"), namespace)  # noqa: S102 -- selected upstream source
    return namespace


class CommandEvent(Event):
    def __init__(self, text, user="admin", group="100", admin=True):
        # AstrBot's wake-prefix stage removes '/' before CommandFilter runs.
        super().__init__(user, group, text.removeprefix("/"))
        self.is_at_or_wake_command = True
        self.admin = admin
        self.extras = {}

    def is_admin(self):
        return self.admin

    def get_message_str(self):
        return self.message_str

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value


@unittest.skipUnless(ASTRBOT_SOURCE, "set ASTRBOT_SOURCE to test upstream filters")
class AstrBotCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.filters = load_filters()
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        cls.plugin_node = next(node for node in tree.body
                               if isinstance(node, ast.ClassDef) and node.name == "DailyLimitPlugin")

    def setUp(self):
        self.plugin = make_plugin()

    def dispatch(self, text, method="limit_reset", **event_args):
        node = next(node for node in self.plugin_node.body if getattr(node, "name", None) == method)
        decorators = [item for item in node.decorator_list if isinstance(item, ast.Call)]
        command = next(item for item in decorators if getattr(item.func, "attr", None) == "command")
        parents = []
        if isinstance(command.func.value, ast.Name) and command.func.value.id == "limit_command_group":
            group = next(node for node in self.plugin_node.body
                         if getattr(node, "name", None) == "limit_command_group")
            parents = [group.decorator_list[0].args[0].value]
        filter_ = self.filters["CommandFilter"](
            command.args[0].value, parent_command_names=parents,
            handler_md=types.SimpleNamespace(handler=getattr(type(self.plugin), method)),
        )
        # Derive guards from the plugin's actual decorators: removing the admin
        # decorator must cause the denied-reset regression to fail.
        for item in decorators:
            if getattr(item.func, "attr", None) == "permission_type":
                permission = getattr(self.filters["PermissionType"], item.args[0].attr)
                filter_.add_custom_filter(self.filters["PermissionTypeFilter"](permission))
        event = CommandEvent(text, **event_args)
        accepted = filter_.filter(event, {})
        if accepted:
            asyncio.run(getattr(self.plugin, method)(event, **event.get_extra("parsed_params")))
        return accepted, event

    def test_group_reset_binds_two_string_arguments_and_preserves_other_targets(self):
        group_key = self.plugin._get_group_key("00100")
        other_key = self.plugin._get_group_key("100")
        self.plugin.redis.set(group_key, 2, ex=3600)
        self.plugin.redis.set(other_key, 1, ex=3600)
        accepted, event = self.dispatch("/limit  reset\tGROUP   00100")
        self.assertTrue(accepted)
        self.assertEqual(event.get_extra("parsed_params"), {"user_id": "GROUP", "group_id": "00100"})
        self.assertIn("已重置群组 00100", event.result)
        self.assertIsNone(self.plugin.redis.get(group_key))
        self.assertEqual(int(self.plugin.redis.get(other_key)), 1)

    def test_user_all_help_and_invalid_arguments_use_real_binding(self):
        for text, expected in [("/limit reset", "重置使用次数命令用法"),
                               ("/limit reset group", "用法: /limit reset group"),
                               ("/limit reset group abc", "群组ID格式错误"),
                               ("/limit reset all 100", "用法:"),
                               ("/limit reset 20 100", "用法:")]:
            with self.subTest(text=text):
                self.assertIn(expected, self.dispatch(text)[1].result)
        user_key = self.plugin._get_user_key("100")
        group_key = self.plugin._get_group_key("100")
        self.plugin.redis.set(user_key, 1, ex=3600)
        self.plugin.redis.set(group_key, 2, ex=3600)
        self.assertIn("已重置用户 100", self.dispatch("/limit reset 100")[1].result)
        self.assertIsNone(self.plugin.redis.get(user_key))
        self.assertEqual(int(self.plugin.redis.get(group_key)), 2)
        self.assertIn("已重置所有使用记录", self.dispatch("/limit reset all")[1].result)
        self.assertIsNone(self.plugin.redis.get(group_key))

    def test_non_admin_cannot_reset_any_target_even_when_exempt(self):
        key = self.plugin._get_group_key("100")
        self.plugin.redis.set(key, 2, ex=3600)
        for user in ["20", "99"]:
            for target in ["group 100", "100", "all"]:
                with self.subTest(user=user, target=target):
                    accepted, event = self.dispatch(f"/limit reset {target}", user=user, admin=False)
                    self.assertFalse(accepted)
                    self.assertIsNone(event.result)
                    self.assertEqual(int(self.plugin.redis.get(key)), 2)

    def test_exempt_status_command_and_query_permission(self):
        self.assertTrue(request(self.plugin, "20", "100")[0])
        accepted, event = self.dispatch("/limit_status", "limit_status", user="99", admin=False)
        self.assertTrue(accepted)
        self.assertIn("豁免用户", event.result)
        self.assertIn("今日已使用：1/2 次", event.result)
        self.assertIn("剩余次数：1 次", event.result)
        self.plugin.config["limits"]["allow_normal_users_check_limit"] = False
        self.plugin.admin_users = ["admin"]
        self.assertEqual(self.dispatch("/limit_status", "limit_status", user="99", admin=False)[1].result,
                         "您没有权限查询使用限制")


@unittest.skipUnless(ASTRBOT_SOURCE and REDIS_SERVER,
                     "set ASTRBOT_SOURCE and DAILYLIMIT_TEST_REDIS_SERVER for real Redis")
class RedisCommandTests(AstrBotCommandTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import redis

        resources = ExitStack()
        cls.addClassCleanup(resources.close)
        directory = resources.enter_context(tempfile.TemporaryDirectory(prefix="dailylimit-redis-"))
        server = shutil.which(REDIS_SERVER)
        if server is None:
            raise RuntimeError(f"Redis server not found: {REDIS_SERVER}")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            cls.port = reservation.getsockname()[1]
        log = resources.enter_context(tempfile.TemporaryFile(mode="w+"))  # noqa: SIM115 -- class-scoped ExitStack owns this file
        cls.server = subprocess.Popen(
            [server, "--bind", "127.0.0.1", "--port", str(cls.port),
             "--save", "", "--appendonly", "no", "--dir", directory],
            stdout=log, stderr=subprocess.STDOUT,
        )
        cls.addClassCleanup(cls.stop_server)
        cls.client = redis.Redis(host="127.0.0.1", port=cls.port, decode_responses=True)
        cls.addClassCleanup(cls.client.close)
        deadline = time.monotonic() + 5
        while True:
            try:
                cls.client.ping()
                break
            except redis.ConnectionError:
                if cls.server.poll() is not None or time.monotonic() >= deadline:
                    log.seek(0)
                    raise RuntimeError("Isolated Redis server did not start:\n" + log.read()) from None
                time.sleep(0.01)

    @classmethod
    def stop_server(cls):
        cls.server.terminate()
        try:
            cls.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.server.kill()
            cls.server.wait(timeout=5)

    def setUp(self):
        super().setUp()
        # This DB belongs to the private, ephemeral server started above.
        self.client.flushdb()
        self.plugin.redis = self.client
        self.plugin.redis_client.redis = self.client

    def test_status_is_read_only_and_group_reset_preserves_records_and_ttl(self):
        self.assertEqual(series(self.plugin, 3, "20", "100"), [True, True, False])
        other = self.plugin._get_user_key("20", "200")
        historical = self.plugin._get_group_key("100").replace("2026-10-09", "2026-10-08")
        for key in [other, historical]:
            self.client.set(key, 7, ex=3600)
        # Compare every key's serialized value, including lists and hashes.
        binary = type(self.client)(host="127.0.0.1", port=self.port)
        self.addCleanup(binary.close)
        before = {key: binary.dump(key) for key in self.client.keys("*")}
        deadlines = {key: self.client.pttl(key) for key in before}
        result = self.dispatch("/limit_status", "limit_status", user="99", admin=False)[1].result
        self.assertIn("剩余次数：0 次", result)
        self.assertEqual(series(self.plugin, 3, "99", "100"), [True] * 3)
        self.assertEqual({key: binary.dump(key) for key in self.client.keys("*")}, before)
        for key, ttl in deadlines.items():
            self.assertLessEqual(self.client.pttl(key), ttl)
            self.assertGreater(self.client.pttl(key), ttl - 5000)
        group_key = self.plugin._get_group_key("100")
        self.assertIn("已重置群组 100", self.dispatch("/limit reset group 100")[1].result)
        expected = {key: value for key, value in before.items() if key != group_key}
        self.assertEqual({key: binary.dump(key) for key in self.client.keys("*")}, expected)
        self.assertTrue(request(self.plugin, "20", "100")[0])
        self.assertEqual(self.client.get(group_key), "1")


if __name__ == "__main__":
    unittest.main()
