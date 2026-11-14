# -*- coding: utf-8 -*-
"""进程内滑动窗口限流器。

* 键是任意 tuple（如 ``(客户端IP, 机器码)``）；
* 每个键维护一个时间戳 deque，超过阈值返回 False（调用方转 429 ``rate_limited``）；
* 每 1000 次调用清理一次过期键，避免长期运行内存增长；
* **多 worker 部署时每个进程独立计数**，因此生产建议 ``--workers 1``
  （本服务 QPS 极低，单进程足够；多 worker 需换 Redis/DB 计数）。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict, Hashable, Tuple

from app.config import get_config


class SlidingWindowLimiter:
    """滑动窗口限流器（线程安全）。"""

    def __init__(self, limit: int, window_sec: int) -> None:
        self.limit: int = max(1, int(limit))
        self.window_sec: int = max(1, int(window_sec))
        self._hits: Dict[Hashable, Deque[float]] = {}
        self._lock = threading.Lock()
        self._calls: int = 0

    def allow(self, key: Hashable) -> bool:
        """记录一次请求并判断是否放行。"""
        now_ts = time.time()
        edge = now_ts - self.window_sec
        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                bucket = deque()
                self._hits[key] = bucket
            while bucket and bucket[0] < edge:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now_ts)
            self._calls += 1
            if self._calls % 1000 == 0:
                self._purge(edge)
            return True

    def reset(self, key: Hashable) -> None:
        """清除某个键的计数（测试 / 人工解封用）。"""
        with self._lock:
            self._hits.pop(key, None)

    def reset_all(self) -> None:
        """清空全部计数（测试用）。"""
        with self._lock:
            self._hits.clear()
            self._calls = 0

    def _purge(self, edge: float) -> None:
        """清理所有已过期键（仅内部调用，需持锁）。"""
        stale = [k for k, v in self._hits.items()
                 if not v or v[-1] < edge]
        for key in stale:
            self._hits.pop(key, None)


# ---------------------------------------------------------------------------
# 模块级单例（阈值来自配置，首次访问时创建）
# ---------------------------------------------------------------------------
_LIMITERS: Dict[str, Tuple[SlidingWindowLimiter, Tuple[int, int]]] = {}
_LIMITERS_LOCK = threading.Lock()


def _get_limiter(name: str) -> SlidingWindowLimiter:
    """按名字取限流器；配置里的 ``次数/秒数`` 变化时重建。"""
    cfg = get_config()
    limits = {
        "activate": cfg.activate_rate_limit,
        "login": cfg.login_rate_limit,
        "query": cfg.query_rate_limit,
    }
    spec = limits.get(name, (10, 60))
    with _LIMITERS_LOCK:
        cached = _LIMITERS.get(name)
        if cached is not None and cached[1] == spec:
            return cached[0]
        limiter = SlidingWindowLimiter(spec[0], spec[1])
        _LIMITERS[name] = (limiter, spec)
        return limiter


def activate_limiter() -> SlidingWindowLimiter:
    """激活接口限流器（键：``(客户端IP, 机器码)``，默认 10 次/60 秒）。"""
    return _get_limiter("activate")


def login_limiter() -> SlidingWindowLimiter:
    """登录接口限流器（键：``客户端IP``，默认 20 次/60 秒）。"""
    return _get_limiter("login")


def query_limiter() -> SlidingWindowLimiter:
    """查询接口限流器（键：``客户端IP``，默认 30 次/60 秒）。"""
    return _get_limiter("query")


def reset_all_limiters() -> None:
    """重置全部限流器（测试用）。"""
    with _LIMITERS_LOCK:
        for limiter, _ in _LIMITERS.values():
            limiter.reset_all()
        _LIMITERS.clear()
