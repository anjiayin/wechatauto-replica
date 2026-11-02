# -*- coding: utf-8 -*-
"""WebSocket 通知中心：事件广播（新商机 / 状态变化 / 未读清零 / 发送结果）。

WS 事件统一 ``{"event": ..., "data": {...}}``（共享约定 2）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Optional, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class Notifier:
    """WebSocket 连接管理器（线程安全；广播从任意线程投递到事件循环）。"""

    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # 未读商机 id 集合（内存态：新商机进入即未读，详情打开即清除）
        self._unread: Set[int] = set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """由应用 lifespan 注入主事件循环（供工作线程投递广播）。"""
        self._loop = loop

    async def connect(self, ws: WebSocket) -> None:
        """接受并登记一个前端 WS 连接。"""
        await ws.accept()
        with self._lock:
            self._clients.add(ws)
        logger.info("WS 客户端接入（当前 %d 个）", len(self._clients))

    def disconnect(self, ws: WebSocket) -> None:
        """移除一个断开的连接。"""
        with self._lock:
            self._clients.discard(ws)

    # ------------------------------------------------------------------
    # 广播
    # ------------------------------------------------------------------
    def broadcast(self, event: str, data: Any) -> None:
        """向全部客户端广播事件（线程安全，可从任意线程调用）。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        payload = {"event": event, "data": data}
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(payload), loop)
        except RuntimeError:  # 事件循环已关闭等
            pass

    async def _broadcast(self, payload: dict) -> None:
        with self._lock:
            clients = tuple(self._clients)
        for ws in clients:
            try:
                await ws.send_json(payload)
            except Exception:
                self.disconnect(ws)

    # ------------------------------------------------------------------
    # 未读集合
    # ------------------------------------------------------------------
    def mark_new_opportunity(self, opportunity_id: int) -> None:
        """新商机 → 记为未读并广播。"""
        self._unread.add(opportunity_id)

    def clear_unread(self, opportunity_id: int) -> bool:
        """清除未读；确实清除了返回 True。"""
        if opportunity_id in self._unread:
            self._unread.discard(opportunity_id)
            self.broadcast("unread_cleared", {"opportunity_id": opportunity_id})
            return True
        return False

    @property
    def unread_count(self) -> int:
        """当前未读商机数。"""
        return len(self._unread)
