"""Small thread-safe Redis substitute for product regression tests.

Values remain Python values, preserving the older fixture's integer counters.
``expiries`` contains absolute Unix deadlines, not original TTL seconds; use
``ttl`` or ``pttl`` to inspect remaining lifetime. All commands expire keys
lazily against the injected ``now() -> float`` clock. This is not Redis or a
Lua interpreter; script support is restricted to the product's counter migration.
"""

import fnmatch
import math
import threading
import time

# Recognize the exact supported script (apart from whitespace), rather than
# interpreting arbitrary Lua or silently accepting a changed migration policy.
_MIGRATION_SCRIPT = """
local old_value = redis.call('GET', KEYS[1])
if not old_value then return 0 end
local new_value = redis.call('GET', KEYS[2])
if not new_value then
    redis.call('RENAME', KEYS[1], KEYS[2])
    return 1
end
local old_ttl = redis.call('PTTL', KEYS[1])
local new_ttl = redis.call('PTTL', KEYS[2])
redis.call('INCRBY', KEYS[2], old_value)
if old_ttl == -1 or new_ttl == -1 then
    redis.call('PERSIST', KEYS[2])
else
    redis.call('PEXPIRE', KEYS[2], math.max(old_ttl, new_ttl))
end
redis.call('DEL', KEYS[1])
return 1
"""

_RESET_MIGRATION_SCRIPT = """
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

_SUPPORTED_MIGRATIONS = {
    "".join(_MIGRATION_SCRIPT.split()): False,
    "".join(_RESET_MIGRATION_SCRIPT.split()): True,
}


class MemoryRedis:
    def __init__(self, now=None):
        self.strings = {}
        self.lists = {}
        self.hashes = {}
        self.expiries = {}
        self._now = now if now is not None else time.time
        self._lock = threading.RLock()

    @staticmethod
    def _name(value):
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    @staticmethod
    def _value(value):
        if value is None:
            raise TypeError("Redis values cannot be None")
        return value.decode("utf-8") if isinstance(value, bytes) else value

    def _drop(self, key):
        exists = any(key in values for values in (self.strings, self.lists, self.hashes))
        for values in (self.strings, self.lists, self.hashes, self.expiries):
            values.pop(key, None)
        return exists

    def _kind(self, key):
        deadline = self.expiries.get(key)
        if deadline is not None and deadline <= self._now():
            self._drop(key)
        for kind, values in (("string", self.strings), ("list", self.lists),
                             ("hash", self.hashes)):
            if key in values:
                return kind
        return "none"

    def _check(self, key, kind):
        actual = self._kind(key)
        if actual not in ("none", kind):
            raise RuntimeError("WRONGTYPE Operation against a key holding the wrong kind of value")
        return actual

    def get(self, key):
        with self._lock:
            key = self._name(key)
            self._check(key, "string")
            return self.strings.get(key)

    def set(self, key, value, ex=None, px=None, nx=False, xx=False, keepttl=False):
        with self._lock:
            key, value = self._name(key), self._value(value)
            if (ex is not None and px is not None) or (keepttl and (ex is not None or px is not None)):
                raise ValueError("SET expiry options are mutually exclusive")
            if nx and xx:
                raise ValueError("SET NX and XX are mutually exclusive")
            lifetime = float(ex) if ex is not None else float(px) / 1000 if px is not None else None
            if lifetime is not None and lifetime <= 0:
                raise ValueError("ERR invalid expire time in 'set' command")
            exists = self._kind(key) != "none"
            if (nx and exists) or (xx and not exists):
                return None
            deadline = self.expiries.get(key) if keepttl else None
            self._drop(key)
            self.strings[key] = value
            if lifetime is not None:
                deadline = self._now() + lifetime
            if deadline is not None:
                self.expiries[key] = deadline
            return True

    def incr(self, key, amount=1):
        with self._lock:
            key = self._name(key)
            self._check(key, "string")
            self.strings[key] = int(self.strings.get(key, 0)) + int(amount)
            return self.strings[key]

    def incrby(self, key, amount=1):
        with self._lock:
            return self.incr(key, amount)

    def rpush(self, key, *values):
        with self._lock:
            key = self._name(key)
            if not values:
                raise TypeError("RPUSH requires a value")
            self._check(key, "list")
            decoded = [self._value(value) for value in values]
            self.lists.setdefault(key, []).extend(decoded)
            return len(self.lists[key])

    def lrange(self, key, start, end):
        with self._lock:
            key = self._name(key)
            self._check(key, "list")
            values = self.lists.get(key, [])
            start, end = int(start), int(end)
            start = max(0, len(values) + start) if start < 0 else start
            end = len(values) + end if end < 0 else end
            return values[start:end + 1] if end >= start else []

    def hincrby(self, key, field, increment=1):
        with self._lock:
            key, field = self._name(key), self._name(field)
            self._check(key, "hash")
            next_value = int(self.hashes.get(key, {}).get(field, 0)) + int(increment)
            values = self.hashes.setdefault(key, {})
            values[field] = next_value
            return values[field]

    def hget(self, key, field):
        with self._lock:
            key, field = self._name(key), self._name(field)
            self._check(key, "hash")
            return self.hashes.get(key, {}).get(field)

    def hset(self, key, field=None, value=None, mapping=None):
        with self._lock:
            key = self._name(key)
            updates = dict(mapping or {})
            if field is not None:
                updates[field] = value
            if not updates:
                raise TypeError("HSET requires a field and value")
            updates = {self._name(name): self._value(item) for name, item in updates.items()}
            self._check(key, "hash")
            values = self.hashes.setdefault(key, {})
            added = sum(name not in values for name in updates)
            values.update(updates)
            return added

    def exists(self, *keys):
        with self._lock:
            return sum(self._kind(self._name(key)) != "none" for key in keys)

    def delete(self, *keys):
        with self._lock:
            deleted = 0
            for key in keys:
                key = self._name(key)
                self._kind(key)
                deleted += self._drop(key)
            return deleted

    def expire(self, key, seconds):
        with self._lock:
            key = self._name(key)
            if self._kind(key) == "none":
                return False
            self.expiries[key] = self._now() + float(seconds)
            self._kind(key)
            return True

    def pexpire(self, key, milliseconds):
        with self._lock:
            return self.expire(key, float(milliseconds) / 1000)

    def ttl(self, key):
        with self._lock:
            key = self._name(key)
            if self._kind(key) == "none":
                return -2
            deadline = self.expiries.get(key)
            return -1 if deadline is None else math.floor(deadline - self._now() + 0.5)

    def pttl(self, key):
        with self._lock:
            key = self._name(key)
            if self._kind(key) == "none":
                return -2
            deadline = self.expiries.get(key)
            # The tolerance removes floating point loss for exact milliseconds
            # represented as large Unix timestamps, e.g. PEXPIRE(key, 1).
            return -1 if deadline is None else math.floor((deadline - self._now()) * 1000 + 0.0001)

    def persist(self, key):
        with self._lock:
            key = self._name(key)
            if self._kind(key) == "none" or key not in self.expiries:
                return False
            del self.expiries[key]
            return True

    def keys(self, pattern="*"):
        with self._lock:
            pattern = self._name(pattern)
            names = set(self.strings) | set(self.lists) | set(self.hashes)
            return sorted(key for key in names if self._kind(key) != "none"
                          and fnmatch.fnmatchcase(key, pattern))

    def scan(self, cursor=0, match=None, count=None, _type=None):
        with self._lock:
            names = self.keys()
            cursor, count = int(cursor), 10 if count is None else int(count)
            if cursor < 0 or count <= 0:
                raise ValueError("SCAN requires a nonnegative cursor and positive count")
            end = min(cursor + count, len(names))
            batch = names[cursor:end]
            if match is not None:
                batch = [key for key in batch if fnmatch.fnmatchcase(key, self._name(match))]
            if _type is not None:
                batch = [key for key in batch if self._kind(key) == self._name(_type)]
            return (end if end < len(names) else 0), batch

    def scan_iter(self, match=None, count=None, _type=None):
        # Snapshotting avoids skips when product migration deletes old keys.
        with self._lock:
            names = self.keys("*" if match is None else match)
            if _type is not None:
                names = [key for key in names if self._kind(key) == self._name(_type)]
        for key in names:
            with self._lock:
                if self._kind(key) == "none":
                    continue
            yield key

    def rename(self, source, destination):
        with self._lock:
            source, destination = self._name(source), self._name(destination)
            kind = self._kind(source)
            if kind == "none":
                raise RuntimeError("ERR no such key")
            if source == destination:
                return True
            self._kind(destination)
            self._drop(destination)
            values = {"string": self.strings, "list": self.lists, "hash": self.hashes}[kind]
            values[destination] = values.pop(source)
            deadline = self.expiries.pop(source, None)
            if deadline is not None:
                self.expiries[destination] = deadline
            return True

    def renamenx(self, source, destination):
        with self._lock:
            source, destination = self._name(source), self._name(destination)
            if self._kind(source) == "none":
                raise RuntimeError("ERR no such key")
            if self._kind(destination) != "none":
                return False
            return self.rename(source, destination)

    @staticmethod
    def _check_script(script):
        if isinstance(script, bytes):
            script = script.decode("utf-8")
        normalized = "".join(script.split())
        if normalized not in _SUPPORTED_MIGRATIONS:
            raise NotImplementedError("MemoryRedis only models the known two-key counter migration")
        return _SUPPORTED_MIGRATIONS[normalized]

    def eval(self, script, numkeys, *keys_and_args):
        """Atomically model the known script; this does not execute Lua.

        Missing targets are renamed and existing integer counters are summed.
        The new script takes an active-period TTL in milliseconds: a positive
        TTL replaces the target lifetime, even when either key was persistent.
        Zero retains historical-key behavior: preserve the renamed deadline or
        retain the greater remaining PTTL; either persistent key wins a merge.
        The old script accepts no arguments and always uses historical behavior.
        """
        with self._lock:
            active_script = self._check_script(script)
            if int(numkeys) != 2 or len(keys_and_args) != 2 + int(active_script):
                raise ValueError("Counter migration requires two keys and the script's exact arguments")
            source, destination = map(self._name, keys_and_args[:2])
            old_value = self.get(source)
            if old_value is None:
                return 0
            active_ttl = float(keys_and_args[2]) if active_script else 0
            if self.get(destination) is None:
                self.rename(source, destination)
                if active_ttl > 0:
                    self.pexpire(destination, active_ttl)
                return 1
            old_ttl, new_ttl = self.pttl(source), self.pttl(destination)
            self.incrby(destination, old_value)
            if active_ttl > 0:
                self.pexpire(destination, active_ttl)
            elif old_ttl == -1 or new_ttl == -1:
                self.persist(destination)
            else:
                self.pexpire(destination, max(old_ttl, new_ttl))
            self.delete(source)
            return 1

    def register_script(self, script):
        with self._lock:
            self._check_script(script)

        def execute(keys=None, args=None, client=None):
            target = self if client is None else client
            keys = list(keys or [])
            return target.eval(script, len(keys), *keys, *(args or []))
        return execute

    def pipeline(self, transaction=True):
        with self._lock:
            return Pipeline(self, transaction)


class Pipeline:
    """Queue commands and hold the Redis lock throughout transaction execution."""

    def __init__(self, redis, transaction=True):
        self.redis = redis
        self.transaction = transaction
        self.actions = []

    def __getattr__(self, name):
        if name.startswith("_") or not callable(getattr(self.redis, name, None)):
            raise AttributeError(name)

        def append(*args, **kwargs):
            with self.redis._lock:
                self.actions.append((name, args, kwargs))
            return self
        return append

    def execute(self, raise_on_error=True):
        with self.redis._lock:
            actions, self.actions = self.actions, []
            results = []
            for name, args, kwargs in actions:
                try:
                    results.append(getattr(self.redis, name)(*args, **kwargs))
                except (RuntimeError, TypeError, ValueError, OverflowError) as error:
                    results.append(error)
            if raise_on_error:
                for result in results:
                    if isinstance(result, Exception):
                        raise result
            return results

    def reset(self):
        with self.redis._lock:
            self.actions.clear()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.reset()
