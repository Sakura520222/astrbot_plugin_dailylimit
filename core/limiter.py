"""
核心限制逻辑模块

负责处理用户/群组的限制逻辑，包括：
- 获取用户/群组限制
- 检查消息是否应忽略
- 时间段限制处理
- 群组模式管理
"""

import datetime


class Limiter:
    """核心限制逻辑类"""

    def __init__(self, plugin):
        """
        初始化限制器

        Args:
            plugin: 插件实例
        """
        self.plugin = plugin
        self.logger = plugin.logger
        self.config = plugin.config
        self.config_mgr = plugin.config_mgr  # 引用配置管理器

    @property
    def lock(self):
        return self.plugin.config_loader.lock

    def should_skip_message(self, message_str):
        """检查消息是否应该忽略处理"""
        with self.lock:
            if not message_str or not self.plugin.skip_patterns:
                return False
            return any(message_str.startswith(pattern) for pattern in self.plugin.skip_patterns)

    def get_group_mode(self, group_id):
        """获取群组的模式配置"""
        with self.lock:
            if not group_id:
                return "individual"  # 私聊默认为独立模式
            return self.plugin.group_modes.get(str(group_id), "shared")

    def parse_time_string(self, time_str):
        """解析时间字符串为时间对象"""
        try:
            return datetime.datetime.strptime(time_str, "%H:%M").time()
        except ValueError:
            return None

    def is_in_time_period(self, current_time_str, start_time_str, end_time_str):
        """检查当前时间是否在指定时间段内"""
        return self.plugin.time_period_mgr.is_in_time_period(
            current_time_str, start_time_str, end_time_str
        )

    def get_current_time_period_limit(self):
        """获取当前时间段适用的限制"""
        return self.plugin.time_period_mgr.get_current_time_period_limit()

    def get_time_period_usage_key(self, user_id, group_id=None, time_period_id=None):
        """获取时间段使用次数的Redis键"""
        return self.plugin.time_period_mgr.get_time_period_usage_key(
            user_id, group_id, time_period_id
        )

    def get_time_period_usage(self, user_id, group_id=None):
        """获取用户在时间段内的使用次数"""
        return self.plugin.time_period_mgr.get_time_period_usage(user_id, group_id)

    def increment_time_period_usage(self, user_id, group_id=None):
        """增加用户在时间段内的使用次数"""
        return self.plugin.time_period_mgr.increment_time_period_usage(user_id, group_id)

    def get_user_limit(self, user_id, group_id=None):
        """获取用户的调用限制次数"""
        with self.lock:
            user_id_str = str(user_id)
            limits = self.config["limits"]

            # Preserve exemption, time-window and priority-user precedence.
            if user_id_str in limits["exempt_users"]:
                return float("inf")
            time_period_limit = self.get_current_time_period_limit()
            if time_period_limit is not None:
                return time_period_limit
            if user_id_str in limits.get("priority_users", []):
                return self.plugin.user_limits.get(user_id_str, limits["default_daily_limit"])
            if user_id_str in self.plugin.user_limits:
                return self.plugin.user_limits[user_id_str]
            if group_id and str(group_id) in self.plugin.group_limits:
                return self.plugin.group_limits[str(group_id)]
            return limits["default_daily_limit"]

    def _get_reset_period_date(self):
        """获取重置周期的日期字符串"""
        return self.plugin.redis_keys.get_reset_period_date()

    def _get_seconds_until_tomorrow(self):
        """获取距离明天的秒数"""
        return self.plugin.redis_keys.get_seconds_until_reset()
