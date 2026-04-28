"""消息去重模块 - TTL 自动过期 + Inflight 并发保护

参考 OpenClaw 的 claim → commit/release 三层去重模式：
1. TTL cache：处理过的消息在 20 分钟内不会被再次处理
2. Inflight guard：同一消息不会被两个并发线程同时处理
3. Text+time 窗口作为第二层保护（在 bot.py 中实现）

使用方式：
    guard = DedupeGuard()
    if not guard.claim("msg_id_xxx"):
        return  # 重复或正在处理中
    try:
        # ... 处理消息 ...
        guard.commit("msg_id_xxx")  # 标记完成
    except Exception:
        guard.release("msg_id_xxx")  # 释放（不加入 cache）
        raise
"""

import time
import threading
from collections import OrderedDict
from typing import Set


class TTLSet:
    """TTL 自动过期的 Set

    元素在添加后经过 ttl_ms 毫秒自动过期。
    超过 max_size 时淘汰最旧元素（FIFO）。
    线程安全。
    """

    def __init__(self, ttl_ms: int = 600_000, max_size: int = 5000):
        """
        Args:
            ttl_ms: 每个 key 的存活时间（毫秒），默认 20 分钟
            max_size: 最大元素数，超过时淘汰最旧元素
        """
        self._ttl_seconds = ttl_ms / 1000.0
        self._max = max_size
        self._data: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def check_and_add(self, key: str) -> bool:
        """检查并添加 key

        Args:
            key: 要检查的 key

        Returns:
            True 如果 key 已存在（重复），False 如果首次添加
        """
        now = time.time()
        with self._lock:
            self._evict(now)
            if key in self._data:
                return True  # 重复
            self._data[key] = now
            if len(self._data) > self._max:
                self._data.popitem(last=False)
            return False

    def peek(self, key: str) -> bool:
        """检查 key 是否存在，但不添加

        Args:
            key: 要检查的 key

        Returns:
            True 如果 key 存在
        """
        now = time.time()
        with self._lock:
            self._evict(now)
            return key in self._data

    def _evict(self, now: float):
        """淘汰过期的 key（必须在持锁时调用）"""
        while self._data:
            _, created_at = next(iter(self._data.items()))
            if now - created_at > self._ttl_seconds:
                self._data.popitem(last=False)
            else:
                break

    def clear(self):
        """清空所有 key"""
        with self._lock:
            self._data.clear()

    @property
    def size(self) -> int:
        """当前元素数量"""
        with self._lock:
            self._evict(time.time())
            return len(self._data)


class InflightGuard:
    """Inflight 并发保护

    确保同一条消息不会同时被两个线程处理。
    使用 claim/release 模式。
    """

    def __init__(self):
        self._inflight: Set[str] = set()
        self._lock = threading.Lock()

    def claim(self, key: str) -> bool:
        """尝试 claim key

        Args:
            key: 要 claim 的 key

        Returns:
            True 表示成功 claim（调用者可继续处理）
            False 表示其他线程正在处理中
        """
        with self._lock:
            if key in self._inflight:
                return False
            self._inflight.add(key)
            return True

    def release(self, key: str):
        """释放 key

        处理完成或异常时调用，其他线程可以重新 claim。
        """
        with self._lock:
            self._inflight.discard(key)

    @property
    def size(self) -> int:
        """当前 inflight 数量"""
        with self._lock:
            return len(self._inflight)


class DedupeGuard:
    """去重守卫：组合 TTLSet + InflightGuard

    三层保护：
    1. TTL cache：已成功处理完成的消息
    2. Inflight：正在处理中的消息（并发保护）
    3. 调用者可在外边再加一层 text+time 去重

    典型使用：
        guard = DedupeGuard()

        # 在事件分发层（handle）中：
        if not guard.claim(msg_id):
            return  # 已在 cache 中或 inflight

        # 在后台处理线程中：
        try:
            # ... 处理消息 ...
            guard.commit(msg_id)   # 成功 → 加入 cache
        except Exception:
            guard.release(msg_id)  # 失败 → 仅从 inflight 释放
            raise
    """

    def __init__(self, ttl_ms: int = 600_000, max_size: int = 5000):
        self._cache = TTLSet(ttl_ms, max_size)
        self._inflight = InflightGuard()

    def claim(self, key: str) -> bool:
        """尝试 claim 一条消息

        1. 检查 TTL cache 中是否已有（已处理过的）
        2. 检查 inflight 中是否已存在（正在处理中的）
        3. 都不存在则加入 inflight

        Args:
            key: 消息唯一标识（如 message_id）

        Returns:
            True 调用者可继续处理
            False 应跳过此消息
        """
        if self._cache.peek(key):
            return False
        return self._inflight.claim(key)

    def commit(self, key: str):
        """标记为已处理完成

        从 inflight 移除，加入 TTL cache。
        之后相同的 key 会因 cache hit 被跳过。
        """
        self._inflight.release(key)
        self._cache.check_and_add(key)

    def release(self, key: str):
        """放弃处理（异常等场景）

        仅从 inflight 移除，不加入 cache。
        之后相同的 key 可以被重新处理。
        """
        self._inflight.release(key)

    @property
    def cache_size(self) -> int:
        return self._cache.size

    @property
    def inflight_size(self) -> int:
        return self._inflight.size
