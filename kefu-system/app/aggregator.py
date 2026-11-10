# -*- coding: utf-8 -*-
"""分析微窗口：按 (会话, 发送人) 缓冲，静默期满后 flush 出"分析单元"。

设计要点（技术方案 1.1，优化后为两级窗口的第一级）：
- Listener 回调线程调用 :meth:`Aggregator.add`，仅入缓冲即返回；
- 独立调度线程每 1 秒检查一次，某缓冲区自最后一条消息起静默超过
  分析微窗口（settings 可覆盖，默认 15 秒）即合并为一条 AnalysisUnit
  交给回调——商机最快可见延迟 ≈ 微窗口 + 1 秒（不再等 5 分钟）；
- O-2 强制 flush 安全阀：缓冲区首条消息年龄超过 微窗口 ×
  ``AGGREGATOR_FORCE_FLUSH_FACTOR`` 时无条件 flush，杜绝持续发言
  （相邻消息间隔始终小于微窗口）导致的分析延迟无上界与缓冲无上限；
- 命中后的商机级合并（180 秒窗口）由 pipeline 负责；
- 非文本消息不进入缓冲（由 wechat_service 记 skipped 日志）。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from app.config import AGGREGATOR_FORCE_FLUSH_FACTOR

logger = logging.getLogger(__name__)

# 调度检查间隔（秒）：到期即 flush 的最大额外延迟
_SCHED_INTERVAL = 1.0


@dataclass
class AnalysisUnit:
    """合并窗口产出的分析单元。"""
    session_username: str
    session_name: str
    session_kind: str                  # group / friend
    sender_wxid: str                   # 缺失时为空串（共享约定 9.2）
    sender_name: str
    window_start: str                  # ISO8601
    window_end: str                    # ISO8601
    merged_count: int
    lines: List[Tuple[str, str]] = field(default_factory=list)  # (HH:MM, 内容)
    # 时序诊断：消息首次进入缓冲的墙钟时间（time.time()）与入队时的
    # 单调时钟（time.monotonic()），用于拆分「合并等待」与「排队等待」
    first_seen_epoch: float = 0.0     # 缓冲创建墙钟（与 enqueued_epoch 同源）
    enqueued_epoch: float = 0.0       # 入队墙钟（算合并等待）
    enqueued_mono: float = 0.0        # 入队单调时钟（算排队等待）

    def merged_text(self) -> str:
        """合并后全文：每行 "[HH:MM] 内容"。"""
        return "\n".join(f"[{hm}] {text}" for hm, text in self.lines)


@dataclass
class _Buffer:
    """单个 (会话, 发送人) 的窗口缓冲。"""
    session_name: str
    session_kind: str
    sender_wxid: str
    sender_name: str
    lines: List[Tuple[str, str]] = field(default_factory=list)
    window_start_epoch: float = 0.0
    last_epoch: float = 0.0            # 最后一条消息时间（静默计时基准）
    first_seen_epoch: float = 0.0      # 缓冲创建墙钟时间（端到端计时起点）

    def count(self) -> int:
        return len(self.lines)


class Aggregator:
    """合并窗口管理器。

    Args:
        flush_callback: 窗口期满后的回调，入参为 AnalysisUnit（由管线入队）。
        window_sec_provider: 每次检查时实时读取合并窗口秒数（settings 可热更）。
    """

    def __init__(self, flush_callback: Callable[[AnalysisUnit], None],
                 window_sec_provider: Optional[Callable[[], int]] = None) -> None:
        self._flush_callback = flush_callback
        self._window_sec_provider = window_sec_provider or (lambda: 60)
        self._buffers: Dict[Tuple[str, str], _Buffer] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def add(self, msg: dict, session_name: str, session_kind: str) -> bool:
        """将一条文本消息加入缓冲；成功加入返回 True。

        Args:
            msg: Listener 回调消息 dict（content/sender_username/create_time）。
        """
        content = (msg.get("content") or "").strip()
        if not content:
            return False
        epoch = float(msg.get("create_time") or 0) or datetime.now().timestamp()
        sender_wxid = str(msg.get("sender_username") or "")
        sender_name = str(msg.get("sender_display_name") or sender_wxid or "未知用户")
        # 发送人 wxid 缺失时合并键退化为昵称（共享约定 9.2）
        sender_key = sender_wxid or sender_name
        key = (str(msg.get("username") or ""), sender_key)
        hm = datetime.fromtimestamp(epoch).strftime("%H:%M")
        with self._lock:
            buf = self._buffers.get(key)
            if buf is None:
                buf = _Buffer(
                    session_name=session_name,
                    session_kind=session_kind,
                    sender_wxid=sender_wxid,
                    sender_name=sender_name,
                    first_seen_epoch=time.time(),
                )
                self._buffers[key] = buf
            buf.lines.append((hm, content))
            if buf.window_start_epoch == 0.0:
                buf.window_start_epoch = epoch
            buf.last_epoch = max(buf.last_epoch, epoch)
        return True

    def start(self) -> None:
        """启动过期调度线程。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="kefu-aggregator", daemon=True
        )
        self._thread.start()
        logger.info("合并窗口调度线程已启动（窗口=%ds）", self._window_sec_provider())

    def stop(self, flush_all: bool = True) -> None:
        """停止调度线程；flush_all=True 时把未到期缓冲立即 flush（退出路径）。"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if flush_all:
            self._flush_expired(force=True)

    def pending_count(self) -> int:
        """当前缓冲区数量（状态展示用）。"""
        with self._lock:
            return len(self._buffers)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _run(self) -> None:
        """调度循环：任何异常不得终止线程（共享约定 4）。"""
        while not self._stop.is_set():
            try:
                self._flush_expired()
            except Exception:
                logger.exception("合并窗口调度异常（已忽略）")
            self._stop.wait(_SCHED_INTERVAL)

    def _flush_expired(self, force: bool = False) -> None:
        """把静默超期（或 force 时全部）的缓冲 flush 为分析单元。

        flush 条件（满足其一）：
        - force=True（退出路径）；
        - 最后一条消息静默超过微窗口（正常路径）；
        - O-2 安全阀：首条消息年龄超过 微窗口 × FORCE_FLUSH_FACTOR
          （持续发言导致静默期一直不满足时兜底，保证分析延迟有上界）。
        """
        window = max(5, int(self._window_sec_provider() or 60))
        force_flush_age = window * AGGREGATOR_FORCE_FLUSH_FACTOR
        now = datetime.now().timestamp()
        ready: List[Tuple[Tuple[str, str], _Buffer]] = []
        with self._lock:
            for key, buf in list(self._buffers.items()):
                if force or (now - buf.last_epoch) >= window or (
                        now - buf.window_start_epoch) >= force_flush_age:
                    ready.append((key, self._buffers.pop(key)))
        for key, buf in ready:
            if not buf.lines:
                continue
            unit = AnalysisUnit(
                session_username=key[0],
                session_name=buf.session_name,
                session_kind=buf.session_kind,
                sender_wxid=buf.sender_wxid,
                sender_name=buf.sender_name,
                window_start=datetime.fromtimestamp(
                    buf.window_start_epoch).isoformat(timespec="seconds"),
                window_end=datetime.fromtimestamp(
                    buf.last_epoch).isoformat(timespec="seconds"),
                merged_count=buf.count(),
                lines=list(buf.lines),
                first_seen_epoch=buf.first_seen_epoch,
            )
            try:
                self._flush_callback(unit)
                logger.debug("flush 分析单元：%s/%s %d 条",
                             unit.session_name, unit.sender_name, unit.merged_count)
            except Exception:
                logger.exception("flush 回调异常（单元已丢弃）：%s", key)
