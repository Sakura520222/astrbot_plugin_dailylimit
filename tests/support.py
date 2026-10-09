"""Dependency-free fixtures running real configuration and request source.

Real source: full DailyLimitPlugin constructor and methods (AST, decorator and
Star base removed), ConfigManager, ConfigLoader, Limiter, TimePeriodManager,
RedisKeys, UsageTracker, MessageBuilder. AstrBot registration/event/message
objects, config persistence, Redis server, logging, security, version check and
unrelated managers are substitutes. Not a real AstrBot integration test.
"""
import ast
import asyncio
import copy
import datetime
import importlib.util
import json
import time
import types
from pathlib import Path

from redis_stub import MemoryRedis

ROOT = Path(__file__).resolve().parents[1]


class FixedDateTime(datetime.datetime):
    current = None

    @classmethod
    def now(cls, tz=None):
        value = cls.current or cls(2026, 10, 9, 12, 0, 0)
        return value if tz is None else value.replace(tzinfo=tz)


CLOCK = types.SimpleNamespace(datetime=FixedDateTime, time=datetime.time,
                              timedelta=datetime.timedelta)


class FakeConfig(dict):
    def __init__(self, contents):
        super().__init__(copy.deepcopy(contents))
        self.saved = []

    def save_config(self):
        self.saved.append(copy.deepcopy(dict(self)))


class FakeLogger:
    def __init__(self, plugin):
        self.entries = []

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.entries.append((name, args))


class FakeRedisClient:
    def __init__(self, plugin):
        self.redis = MemoryRedis(now=lambda: FixedDateTime.now().timestamp())

    def init_redis(self):
        return True

    def validate_redis_connection(self):
        return True


class OtherManager:
    def __init__(self, plugin):
        self.abuse_records = {}
        self.blocked_users = {}
        self.abuse_stats = {}
        self.anti_abuse_enabled = False

    def init_version_check(self):
        pass


class FakeStar:
    def __init__(self, context):
        self.context = context


class Message:
    def __init__(self):
        self.text = ""

    def message(self, text):
        self.text = text
        return self

    def at(self, name, user_id):
        return self


class MessageType:
    GROUP_MESSAGE = "group"


class Event:
    def __init__(self, user_id="20", group_id=None, text="hello"):
        self.user_id = user_id
        self.group_id = group_id
        self.message_str = text
        self.stopped = False
        self.sent = []
        self.result = None

    def get_sender_id(self):
        return self.user_id

    def get_group_id(self):
        return self.group_id

    def get_message_type(self):
        return MessageType.GROUP_MESSAGE if self.group_id is not None else "private"

    def get_sender_name(self):
        return "tester"

    def stop_event(self):
        self.stopped = True

    async def send(self, message):
        self.sent.append(message.text)

    def set_result(self, result):
        self.result = result.text


def source_class(root, name, class_name):
    path = root / "core" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"review_{root.name}_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if hasattr(module, "datetime"):
        module.datetime = CLOCK
    return getattr(module, class_name)


def plugin_class(root):
    tree = ast.parse((root / "main.py").read_text(), filename=str(root / "main.py"))
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                    and node.name == "DailyLimitPlugin")
    body = [node for node in original.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for node in body:
        node.decorator_list = []
    extracted = ast.ClassDef(name="DailyLimitPlugin", bases=[ast.Name(id="FakeStar", ctx=ast.Load())],
                             keywords=[], body=body, decorator_list=[])
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              extracted], type_ignores=[])
    namespace = {"FakeStar": FakeStar, "datetime": CLOCK, "time": time,
                 "json": json, "Logger": FakeLogger, "RedisClient": FakeRedisClient,
                 "WebServer": None, "MessageType": MessageType,
                 "MessageChain": Message, "MessageEventResult": Message}
    for filename, classname in [("config_manager", "ConfigManager"),
                                ("config_loader", "ConfigLoader"), ("limiter", "Limiter"),
                                ("usage_tracker", "UsageTracker"), ("message_builder", "MessageBuilder"),
                                ("time_period_manager", "TimePeriodManager"), ("redis_keys", "RedisKeys")]:
        namespace[classname] = source_class(root, filename, classname)
    for classname in ["Security", "VersionChecker", "HelpManager", "StatsAnalyzer", "WebManager",
                      "SecurityHandler", "MessagesHandler"]:
        namespace[classname] = OtherManager
    exec(compile(ast.fix_missing_locations(module), str(root / "main.py"), "exec"), namespace)  # noqa: S102 -- trusted repository source
    return namespace["DailyLimitPlugin"]


DEFAULT_CONFIG = {
    "limits": {"default_daily_limit": 5, "exempt_users": ["99"],
               "priority_users": ["12", "13"], "group_limits": "100:2\n200:3",
               "user_limits": "11:4\n12:3", "group_mode_settings": "200:individual",
               "time_period_limits": "", "skip_patterns": ["#", "*"],
               "daily_reset_time": "00:00", "custom_messages": {"zero_usage_reminder_enabled": False}},
    "security": {"anti_abuse_enabled": False}, "redis": {},
}


def make_plugin(updates=None, at=None):
    FixedDateTime.current = at or FixedDateTime(2026, 10, 9, 12, 0, 0)
    config = copy.deepcopy(DEFAULT_CONFIG)
    if updates:
        config["limits"].update(updates)
    return plugin_class(ROOT)(None, FakeConfig(config))


def request(plugin, user="20", group=None, text="hello", prompt="hello"):
    event = Event(user, group, text)
    allowed = asyncio.run(plugin.on_llm_request(event, types.SimpleNamespace(prompt=prompt)))
    return allowed, event


def series(plugin, n, user="20", group=None):
    return [request(plugin, user, group)[0] for _ in range(n)]


def web_save(plugin, updates):
    """Run WebServer's source update/save/reload methods without Flask or HTTP."""
    path = ROOT / "web_server.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                    and node.name == "WebServer")
    names = {"_update_config", "_validate_config_data", "_get_config_data", "_update_redis_config",
             "_update_limits_config", "_update_default_daily_limit", "_update_user_list",
             "_update_string_config", "_update_list_config", "_update_custom_messages",
             "_finalize_config_update"}
    body = [node for node in original.body if isinstance(node, ast.FunctionDef)
            and node.name in names]
    extracted = ast.ClassDef(name="WebConfig", bases=[], keywords=[], body=body,
                             decorator_list=[])
    module = ast.Module(body=[extracted], type_ignores=[])
    namespace = {"copy": copy}
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)  # noqa: S102 -- trusted repository source
    web = namespace["WebConfig"]()
    web.plugin = plugin
    return web._update_config(updates)
