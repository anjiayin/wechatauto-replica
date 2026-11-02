# -*- coding: utf-8 -*-
"""分析管线：有界队列 + worker 池 + 重试 + 落库 + 触发通知。

流程（技术方案 4.1）：
    Aggregator flush → enqueue（有界 500，满则丢弃+计数告警）
    → worker 取单元 → 预筛（可选）→ AI 判定（失败退避重试 2 次）
    → hit 且 confidence ≥ 门槛：INSERT opportunities + analysis_logs(ok)
      + WS 广播 opportunity_new
    → hit 但低于门槛：analysis_logs(below_threshold)
    → 失败：analysis_logs(error)

任何异常不得终止 worker 线程（共享约定 4）。
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Optional

from app import analyzer, db as dbmod
from app.aggregator import AnalysisUnit
from app.config import (
    QUEUE_MAX,
    RETRY_BACKOFF_BASE,
    RETRY_TIMES,
    WORKER_COUNT,
)
from app.crypto import decrypt_api_key
from app.db import Database
from app.notifier import Notifier

logger = logging.getLogger(__name__)


class Pipeline:
    """商机分析管线（应用生命周期内单例）。"""

    def __init__(self, database: Database, notifier: Notifier,
                 worker_count: int = WORKER_COUNT, queue_max: int = QUEUE_MAX,
                 client_factory: Optional[object] = None) -> None:
        """Args:
            client_factory: ``analyzer.AIClient`` 工厂（测试可注入替身），
                签名 ``(ai_cfg: dict) -> AIClient``；None 时按配置创建。
        """
        self._db = database
        self._notifier = notifier
        self._worker_count = worker_count
        self._client_factory = client_factory or self._default_client_factory
        self._queue: "queue.Queue[Optional[AnalysisUnit]]" = queue.Queue(
            maxsize=queue_max
        )
        self.queue_max = queue_max
        self.dropped_total = 0
        self._busy = 0
        self._busy_lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动固定 worker 池。"""
        if self._workers:
            return
        self._stop.clear()
        for i in range(self._worker_count):
            t = threading.Thread(target=self._worker_run,
                                 name=f"kefu-worker-{i}", daemon=True)
            t.start()
            self._workers.append(t)
        logger.info("分析管线已启动（worker=%d, 队列上限=%d）",
                    len(self._workers), self.queue_max)

    def stop(self) -> None:
        """停止 worker 池（投递哨兵并等待退出）。"""
        self._stop.set()
        for _ in self._workers:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        for t in self._workers:
            t.join(timeout=5)
        self._workers.clear()
        logger.info("分析管线已停止")

    # ------------------------------------------------------------------
    # 入队
    # ------------------------------------------------------------------
    def enqueue(self, unit: AnalysisUnit) -> bool:
        """分析单元入队（非阻塞）；队列满时丢弃并计数告警。"""
        try:
            self._queue.put_nowait(unit)
            return True
        except queue.Full:
            self.dropped_total += 1
            logger.warning("分析队列已满（%d），丢弃单元：%s/%s（累计丢弃 %d）",
                           self.queue_max, unit.session_name,
                           unit.sender_name, self.dropped_total)
            return False

    # ------------------------------------------------------------------
    # worker
    # ------------------------------------------------------------------
    def _worker_run(self) -> None:
        """worker 主循环：任何异常不得终止线程。"""
        while not self._stop.is_set():
            try:
                unit = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if unit is None:
                break
            with self._busy_lock:
                self._busy += 1
            try:
                self._process(unit)
            except Exception:
                logger.exception("处理分析单元时发生未预期异常（已忽略）")
            finally:
                with self._busy_lock:
                    self._busy -= 1

    @property
    def busy(self) -> int:
        """正在分析的 worker 数。"""
        with self._busy_lock:
            return self._busy

    @property
    def queue_size(self) -> int:
        """当前队列长度。"""
        return self._queue.qsize()

    # ------------------------------------------------------------------
    # 单元处理
    # ------------------------------------------------------------------
    def _process(self, unit: AnalysisUnit) -> None:
        """预筛 → AI 判定（重试）→ 落库 → 广播。"""
        analysis_cfg = dbmod.read_analysis_settings(self._db)
        base_log = dict(
            session_username=unit.session_username,
            sender_wxid=unit.sender_wxid,
            msg_count=unit.merged_count,
            input_text=unit.merged_text()[:2000],
        )
        # 合规未确认：不启动分析（共享约定/待明确事项 4）
        if not analysis_cfg.get("compliance_confirmed"):
            self._db.insert_analysis_log(
                status="skipped", error="未完成合规知情确认，分析未启动",
                model="", **base_log)
            return
        unit_text = unit.merged_text()
        # 轻量预筛（可选）
        if analysis_cfg.get("prescreen_enabled"):
            if not analyzer.prescreen(unit_text,
                                      analysis_cfg.get("prescreen_keywords", ""),
                                      analysis_cfg.get("prescreen_regex", "")):
                self._db.insert_analysis_log(status="skipped",
                                             error="预筛未命中", model="",
                                             **base_log)
                return
        # AI 配置检查
        ai_cfg = dbmod.read_ai_settings(self._db)
        api_key = decrypt_api_key(ai_cfg.get("api_key_enc", ""))
        if not ai_cfg.get("base_url") or not api_key or not ai_cfg.get("model"):
            self._db.insert_analysis_log(status="error",
                                         error="AI 接口未配置完整，请在设置页配置",
                                         model="", **base_log)
            return
        client = self._build_client(ai_cfg, api_key)
        # 调用 AI（失败指数退避重试 RETRY_TIMES 次）
        types = list(dbmod.iter_enabled_types(self._db))
        result: Optional[dict] = None
        last_err = ""
        for attempt in range(1 + RETRY_TIMES):
            try:
                started = time.monotonic()
                result = analyzer.analyze_unit(
                    client, unit_text, unit.session_name, unit.session_kind,
                    unit.sender_name, unit.sender_wxid, unit.merged_count,
                    types, analysis_cfg.get("strictness", "balanced"),
                )
                latency_ms = int((time.monotonic() - started) * 1000)
                self._on_result(unit, result, latency_ms, base_log,
                                ai_cfg.get("model", ""))
                return
            except analyzer.AIError as exc:
                last_err = str(exc)
                wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                logger.warning("AI 调用失败（第 %d 次）：%s，%.0fs 后重试",
                               attempt + 1, exc, wait)
                if attempt < RETRY_TIMES:
                    time.sleep(wait)
        # 最终失败
        self._db.insert_analysis_log(status="error", error=last_err,
                                     model=ai_cfg.get("model", ""), **base_log)

    def _build_client(self, ai_cfg: dict, api_key: str):
        """按配置构建 AI 客户端。"""
        try:
            return self._client_factory({
                "base_url": ai_cfg.get("base_url", ""),
                "api_key": api_key,
                "model": ai_cfg.get("model", ""),
                "timeout_sec": int(float(ai_cfg.get("timeout_sec", "60"))),
                "temperature": float(ai_cfg.get("temperature", "0.2")),
            })
        except (TypeError, ValueError):
            return self._client_factory({
                "base_url": ai_cfg.get("base_url", ""),
                "api_key": api_key,
                "model": ai_cfg.get("model", ""),
                "timeout_sec": 60,
                "temperature": 0.2,
            })

    @staticmethod
    def _default_client_factory(cfg: dict):
        """默认 AIClient 工厂。"""
        return analyzer.AIClient(
            base_url=cfg["base_url"], api_key=cfg["api_key"],
            model=cfg["model"], timeout_sec=cfg["timeout_sec"],
            temperature=cfg["temperature"],
        )

    # ------------------------------------------------------------------
    # 结果处理
    # ------------------------------------------------------------------
    def _on_result(self, unit: AnalysisUnit, result: dict, latency_ms: int,
                   base_log: dict, model: str) -> None:
        """按命中/门槛/未命中分流落库与广播。"""
        result_json = json.dumps(result, ensure_ascii=False)
        if not result.get("hit"):
            self._db.insert_analysis_log(
                status="ok", result=result_json, model=model,
                latency_ms=latency_ms, **base_log)
            return
        min_conf = int(dbmod.read_analysis_settings(self._db).get(
            "min_confidence", 60))
        if result["confidence"] < min_conf:
            self._db.insert_analysis_log(
                status="below_threshold", result=result_json, model=model,
                latency_ms=latency_ms, **base_log)
            return
        # 命中类型：AI 返回类型名 → 匹配已启用类型拿 type_id（找不到则存快照）
        type_name = result.get("type") or "未知类型"
        type_id: Optional[int] = None
        for t in dbmod.iter_enabled_types(self._db):
            if t.get("name") == type_name:
                type_id = int(t["id"])
                break
        opp = self._db.insert_opportunity(
            session_username=unit.session_username,
            session_name=unit.session_name,
            sender_wxid=unit.sender_wxid,
            sender_name=unit.sender_name,
            window_start=unit.window_start,
            window_end=unit.window_end,
            merged_count=unit.merged_count,
            raw_text=unit.merged_text(),
            type_id=type_id,
            type_name=type_name,
            summary=analyzer.summarize_points(result.get("points", [])),
            key_quote=result.get("quote", ""),
            confidence=result["confidence"],
        )
        self._db.insert_analysis_log(
            status="ok", result=result_json, model=model,
            latency_ms=latency_ms, opportunity_id=opp["id"], **base_log)
        self._notifier.mark_new_opportunity(opp["id"])
        self._notifier.broadcast("opportunity_new", opp)
        logger.info("新商机 #%d：%s/%s（%s，置信度 %d）",
                    opp["id"], unit.session_name, unit.sender_name,
                    type_name, result["confidence"])
