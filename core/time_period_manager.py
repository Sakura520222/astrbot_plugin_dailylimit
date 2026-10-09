"""
时间段管理模块

负责时间段限制的解析、验证和管理，包括：
- 时间段配置解析
- 时间格式验证
- 时间段使用次数管理
- 时间段限制查询
"""

import datetime
import hashlib
import json
import re


class TimePeriodManager:
    """时间段管理类"""

    LEGACY_IDS_KEY = "astrbot:time_period_limit:legacy_ids:v2"
    LEGACY_KEY = re.compile(r"^astrbot:time_period_limit:([^:]+):(\d+):(.+)$")
    MIGRATE_COUNTER = """
local old_value = redis.call('GET', KEYS[1])
if not old_value then return 0 end
local reset_ttl = tonumber(ARGV[1])
local new_value = redis.call('GET', KEYS[2])
if not new_value then
    redis.call('RENAME', KEYS[1], KEYS[2])
    if reset_ttl > 0 then redis.call('PEXPIRE', KEYS[2], reset_ttl) end
    return 1
end
local old_ttl = redis.call('PTTL', KEYS[1])
local new_ttl = redis.call('PTTL', KEYS[2])
redis.call('INCRBY', KEYS[2], old_value)
if reset_ttl > 0 then
    redis.call('PEXPIRE', KEYS[2], reset_ttl)
elseif old_ttl == -1 or new_ttl == -1 then
    redis.call('PERSIST', KEYS[2])
else
    redis.call('PEXPIRE', KEYS[2], math.max(old_ttl, new_ttl))
end
redis.call('DEL', KEYS[1])
return 1
"""

    def __init__(self, plugin):
        """
        初始化时间段管理器

        Args:
            plugin: 插件实例
        """
        self.plugin = plugin
        self.config = plugin.config
        self.logger = plugin.logger
        self._migration_checked_redis = None

    @property
    def lock(self):
        """All rule readers and writers use the loader's reentrant lock."""
        return self.plugin.config_loader.lock

    @property
    def time_period_limits(self):
        """Return a snapshot instead of retaining a list replaced during reload."""
        with self.lock:
            return [dict(period) for period in self.plugin.time_period_limits]

    def parse_time_period_limits(self, limits_config=None):
        """解析时间段限制配置

        Args:
            limits_config: 时间段限制配置，如果为None则从插件配置读取

        Returns:
            list: 解析后的时间段限制列表
        """
        if limits_config is None:
            with self.lock:
                limits_config = self.config["limits"].get("time_period_limits", "")
                if isinstance(limits_config, list):
                    limits_config = list(limits_config)

        # 处理配置值，兼容字符串和列表两种格式
        if isinstance(limits_config, str):
            # 如果是字符串，按换行符分割并过滤空值
            lines = [
                line.strip()
                for line in limits_config.strip().split("\n")
                if line.strip()
            ]
        elif isinstance(limits_config, list):
            # 如果是列表，确保所有元素都是字符串并过滤空值
            lines = [
                str(line).strip() for line in limits_config if str(line).strip()
            ]
        else:
            # 其他类型，转换为字符串处理
            lines = [str(limits_config).strip()]

        parsed_limits = []
        for line in lines:
            parsed = self.parse_time_period_line(line)
            if parsed:
                parsed_limits.append(parsed)

        return parsed_limits

    def parse_time_period_line(self, line):
        """解析单行时间段限制配置

        Args:
            line: 配置行，格式为 "HH:MM-HH:MM:次数[:启用标志]"

        Returns:
            dict: 解析后的时间段限制，如果解析失败返回None
        """
        # 解析时间范围部分
        time_range_data = self.parse_time_range_from_line(line)
        if not time_range_data:
            return None

        # 解析限制次数
        limit_data = self.parse_limit_from_line(line)
        if limit_data is None:
            return None

        # 解析启用标志
        enabled = self.parse_enabled_flag_from_line(line)

        # Keep disabled entries too: legacy indexes and command indexes depend
        # on the saved order, including disabled rules.
        return {
            "start_time": time_range_data["start_time"],
            "end_time": time_range_data["end_time"],
            "limit": limit_data,
            "enabled": enabled,
        }

    def _split_time_period_line(self, line):
        """Separate fields without splitting the colons inside HH:MM."""
        match = re.fullmatch(
            r"\s*(\d{1,2}:\d{1,2})\s*-\s*(\d{1,2}:\d{1,2})\s*:\s*([+-]?\d+)"
            r"(?:\s*:\s*([^:]*))?\s*",
            line,
        )
        return match.groups() if match else None

    def parse_time_range_from_line(self, line):
        """从配置行中解析时间范围

        Args:
            line: 配置行

        Returns:
            dict: 包含start_time和end_time的字典，解析失败返回None
        """
        parts = self._split_time_period_line(line)
        if not parts:
            return None

        start_time = parts[0].strip()
        end_time = parts[1].strip()

        # 验证时间格式
        if not self.validate_time_format(start_time) or not self.validate_time_format(end_time):
            self.logger.log_warning("时间段限制时间格式错误: {}", line)
            return None

        return {"start_time": start_time, "end_time": end_time}

    def parse_limit_from_line(self, line):
        """从配置行中解析限制次数

        Args:
            line: 配置行

        Returns:
            int: 限制次数，解析失败返回None
        """
        parts = self._split_time_period_line(line)
        if not parts:
            return None

        limit = self._safe_parse_int(parts[2].strip())
        if limit is not None:
            return limit
        else:
            self.logger.log_warning("时间段限制次数格式错误: {}", line)
            return None

    def parse_enabled_flag_from_line(self, line):
        """从配置行中解析启用标志

        Args:
            line: 配置行

        Returns:
            bool: 是否启用
        """
        parts = self._split_time_period_line(line)
        return self._parse_enabled_flag(parts[3]) if parts else True

    def validate_time_format(self, time_str):
        """验证时间格式

        Args:
            time_str: 时间字符串，格式应为HH:MM

        Returns:
            bool: 时间格式是否有效
        """
        try:
            datetime.datetime.strptime(time_str, "%H:%M")
            return True
        except ValueError:
            return False

    def _parse_enabled_flag(self, enabled_str):
        """解析启用标志

        Args:
            enabled_str: 启用标志字符串

        Returns:
            bool: 是否启用
        """
        if enabled_str is None:
            return True

        enabled_str = enabled_str.strip().lower()
        return enabled_str in ["true", "1", "yes", "y"]

    def _validate_config_line(self, line, separator, expected_parts):
        """验证配置行格式

        Args:
            line: 配置行
            separator: 分隔符
            expected_parts: 期望的分割部分数量

        Returns:
            list: 分割后的部分，验证失败返回None
        """
        parts = line.split(separator)
        if len(parts) < expected_parts:
            return None
        return parts

    def _safe_parse_int(self, value):
        """安全地解析整数

        Args:
            value: 要解析的值

        Returns:
            int: 解析后的整数，失败返回None
        """
        try:
            return int(value)
        except (ValueError, TypeError):
            return None

    def format_time_period(self, period):
        """格式化时间段为可读字符串

        Args:
            period: 时间段字典，包含start_time和end_time

        Returns:
            str: 格式化后的时间段字符串
        """
        start_time = period.get("start_time", "")
        end_time = period.get("end_time", "")
        limit = period.get("limit", 0)
        return f"{start_time}-{end_time} ({limit}次)"

    def is_in_time_period(self, current_time_str, start_time_str, end_time_str):
        """检查当前时间是否在指定时间段内

        Args:
            current_time_str: 当前时间字符串，格式HH:MM
            start_time_str: 开始时间字符串，格式HH:MM
            end_time_str: 结束时间字符串，格式HH:MM

        Returns:
            bool: 是否在时间段内
        """
        try:
            current_time = datetime.datetime.strptime(current_time_str, "%H:%M")
            start_time = datetime.datetime.strptime(start_time_str, "%H:%M")
            end_time = datetime.datetime.strptime(end_time_str, "%H:%M")

            if start_time <= end_time:
                return start_time <= current_time <= end_time
            return current_time >= start_time or current_time <= end_time
        except (ValueError, TypeError):
            return False

    def get_current_time_period(self, current_time_str=None):
        """Choose one enabled window for limits, counts and status displays."""
        with self.lock:
            if current_time_str is None:
                current_time_str = datetime.datetime.now().strftime("%H:%M")
            for period in self.plugin.time_period_limits:
                if period.get("enabled", True) and self.is_in_time_period(
                    current_time_str, period["start_time"], period["end_time"]
                ):
                    return dict(period)
            return None

    def get_time_period_id(self, period):
        """A window keeps its budget when reordered, disabled or edited."""
        start = self._normalize_time(period["start_time"])
        end = self._normalize_time(period["end_time"])
        digest = hashlib.sha256(f"{start}-{end}".encode()).hexdigest()[:24]
        return f"v2_{digest}"

    def _normalize_time(self, time_str):
        hour, minute = map(int, time_str.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"时间格式错误: {time_str}")
        return f"{hour:02d}:{minute:02d}"

    def _redis(self):
        client = getattr(self.plugin, "redis_client", None)
        return getattr(client, "redis", None) if client else None

    def before_time_period_change(self, require_redis=True):
        """Migrate while old indexes still identify the original windows.

        Call under the loader lock before every list mutation or replacement.
        Startup may defer migration until the Redis connection is initialized.
        """
        with self.lock:
            redis = self._redis()
            if redis is None:
                if require_redis:
                    raise RuntimeError("Redis不可用，无法在修改时段规则前保留旧计数")
                return 0
            return self._migrate_legacy_counts(redis)

    def ensure_legacy_counts(self):
        """Run before quota reads so migration errors cannot become zero usage."""
        with self.lock:
            redis = self._redis()
            if redis is None:
                raise RuntimeError("Redis不可用，无法验证旧时段计数")
            if self._migration_checked_redis is redis:
                return 0
            return self._migrate_legacy_counts(redis)

    def _migrate_legacy_counts(self, redis):
        """Freeze the original mapping and move each counter atomically.

        The permanent sidecar lets a restart finish an interrupted migration
        after rules have been reordered. Mixed old/new plugin writers are not
        supported because an old index has no recoverable window identity.
        """
        legacy = []
        for raw_key in redis.scan_iter(match="astrbot:time_period_limit:*"):
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            match = self.LEGACY_KEY.fullmatch(key)
            if match:
                legacy.append((key, match.groups()))
        if not legacy:
            self._align_current_counter_expiry(redis)
            self._migration_checked_redis = redis
            return 0

        encoded_mapping = redis.get(self.LEGACY_IDS_KEY)
        if encoded_mapping is None:
            mapping = {
                str(index): self.get_time_period_id(period)
                for index, period in enumerate(self.plugin.time_period_limits)
            }
            if any(index not in mapping for _, (_, index, _) in legacy):
                raise RuntimeError("旧时段计数的索引无法匹配已加载规则，拒绝重新分配额度")
            redis.set(self.LEGACY_IDS_KEY, json.dumps(mapping, sort_keys=True), nx=True)
            encoded_mapping = redis.get(self.LEGACY_IDS_KEY)
        mapping = json.loads(encoded_mapping)
        if not isinstance(mapping, dict) or any(
            not isinstance(identity, str) or not re.fullmatch(r"v2_[0-9a-f]{24}", identity)
            for identity in mapping.values()
        ):
            raise RuntimeError("旧时段计数映射无效，拒绝重新分配额度")
        if any(index not in mapping for _, (_, index, _) in legacy):
            raise RuntimeError("旧时段计数不在已保存的迁移映射中，拒绝重新分配额度")

        reset_date = self.plugin.redis_keys.get_reset_period_date()
        calendar_date = datetime.datetime.now().strftime("%Y-%m-%d")
        reset_ttl = max(1, self.plugin.redis_keys.get_seconds_until_reset()) * 1000
        migrated = 0
        for old_key, (date, index, suffix) in legacy:
            # Old Limiter used calendar days, while the other counter path used
            # reset periods. Carry both active buckets into the same budget.
            active = date in (calendar_date, reset_date)
            if active:
                date = reset_date
            new_key = f"astrbot:time_period_limit:{date}:{mapping[index]}:{suffix}"
            migrated += int(redis.eval(
                self.MIGRATE_COUNTER, 2, old_key, new_key, reset_ttl if active else 0
            ))
        self._align_current_counter_expiry(redis)
        self._migration_checked_redis = redis
        return migrated

    def _align_current_counter_expiry(self, redis):
        """Repair short-lived v2 counters from an earlier interrupted upgrade.

        Requests that are already exhausted do not increment and cannot repair
        their TTL there. Only the current reset-period keys are adjusted; their
        values and historical buckets remain untouched.
        """
        date = self.plugin.redis_keys.get_reset_period_date()
        milliseconds = max(1, self.plugin.redis_keys.get_seconds_until_reset()) * 1000
        for key in redis.scan_iter(match=f"astrbot:time_period_limit:{date}:v2_*:*"):
            redis.pexpire(key, milliseconds)

    def get_current_time_period_limit(self):
        """获取当前时间段适用的限制

        Returns:
            int: 当前时间段的限制次数，如果不在任何时间段内返回None
        """
        period = self.get_current_time_period()
        return period["limit"] if period else None

    def get_time_period_usage_key(self, user_id, group_id=None, time_period_id=None):
        """获取时间段使用次数的Redis键

        Args:
            user_id: 用户ID
            group_id: 群组ID（可选）
            time_period_id: 时间段ID（可选），如果为None则使用当前时间段

        Returns:
            str: Redis键，如果当前不在任何时间段内返回None
        """
        with self.lock:
            if time_period_id is None:
                period = self.get_current_time_period()
                if period is None:
                    return None
                time_period_id = self.get_time_period_id(period)
            elif str(time_period_id).isdigit():
                index = int(time_period_id)
                if index < len(self.plugin.time_period_limits):
                    time_period_id = self.get_time_period_id(self.plugin.time_period_limits[index])

            if self._redis() is not None:
                self.ensure_legacy_counts()
            if group_id is None:
                group_id = "private_chat"
            date_str = self.plugin.redis_keys.get_reset_period_date()
            return f"astrbot:time_period_limit:{date_str}:{time_period_id}:{group_id}:{user_id}"

    def get_time_period_usage(self, user_id, group_id=None):
        """获取用户在时间段内的使用次数

        Args:
            user_id: 用户ID
            group_id: 群组ID（可选）

        Returns:
            int: 使用次数
        """
        with self.lock:
            redis = self._redis()
            if redis is None:
                return 0
            key = self.get_time_period_usage_key(user_id, group_id)
            if key is None:
                return 0
            usage = redis.get(key)
            return int(usage) if usage else 0

    def increment_time_period_usage(self, user_id, group_id=None):
        """增加用户在时间段内的使用次数

        Args:
            user_id: 用户ID
            group_id: 群组ID（可选）

        Returns:
            bool: 是否成功增加
        """
        with self.lock:
            redis = self._redis()
            if redis is None:
                return False
            key = self.get_time_period_usage_key(user_id, group_id)
            if key is None:
                return False
            pipe = redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, self.plugin.redis_keys.get_seconds_until_reset())
            pipe.execute()
            return True
