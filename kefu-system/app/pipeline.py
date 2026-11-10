# -*- coding: utf-8 -*-
"""分析管线：有界队列 + worker 池 + 重试 + 落库 + 触发通知。

流程（技术方案 4.1，优化后为两级窗口的第二级）：
    Aggregator flush → enqueue（有界 500，满则丢弃+计数告警）
    → worker 取单元 → 预筛（可选）→ AI 判定（失败退避重试 2 次）
    → hit 且 confidence ≥ 门槛：
        同会话同发送人的 pending 商机在商机合并窗口内（默认 180 秒）
        → 合并进该条（raw_text 追加 / window_end 更新 / points 并集去重 /
          summary 用最新要点 / key_quote 取最新 / confidence 取较大），
          写 analysis_logs（opportunity_id 关联被合并商机）并广播
          opportunity_update
        否则 INSERT opportunities + analysis_logs(ok) + 广播 opportunity_new
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
from datetime import datetime, timedelta
from typing import Optional

from app import analyzer, db as dbmod
from app.aggregator import AnalysisUnit
from app.config import (
    AGGREGATOR_FORCE_FLUSH_FACTOR,
    MAX_OPPORTUNITY_SPAN_SEC,
    OPP_MERGE_WINDOW_SEC_DEFAULT,
    QUEUE_MAX,
    RETRY_BACKOFF_BASE,
    RETRY_TIMES,
    WORKER_COUNT,
)
from app.crypto import decrypt_api_key
from app.db import Database
from app.notifier import Notifier

logger = logging.getLogger(__name__)


def _elapsed_ms(later: float, earlier: float) -> Optional[int]:
    """两时间点差值转毫秒；任一缺失/倒挂（时钟调整）时返回 None。"""
    if not later or not earlier:
        return None
    delta = (later - earlier) * 1000.0
    return int(delta) if delta >= 0 else None


def friendly_ai_error(raw: str) -> str:
    """把 AI 调用错误摘要映射为用户可读的中文提示（优化 10）。

    规则：401/403 → Key 无效或无权限；429 → 额度不足或触发限流；
    超时 → 接口超时；其余显示原始错误前 80 字。
    """
    text = str(raw or "").strip()
    lowered = text.lower()
    if "401" in text or "403" in text:
        return "Key 无效或无权限"
    if "429" in text:
        return "额度不足或触发限流"
    if "timeout" in lowered or "timed out" in lowered or "超时" in text:
        return "接口超时"
    return text[:80] if text else "未知错误"


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
        # AI 分析健康度（内存态：/api/status.analysis_health 数据源）
        self._health_lock = threading.Lock()
        self._consecutive_failures = 0
        self._last_error = ""

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
        unit.enqueued_mono = time.monotonic()
        unit.enqueued_epoch = time.time()
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
    # AI 分析健康度（优化 10）
    # ------------------------------------------------------------------
    @property
    def analysis_health(self) -> dict:
        """AI 分析健康度快照（/api/status 数据源）。"""
        with self._health_lock:
            return {"last_error": self._last_error,
                    "consecutive_failures": self._consecutive_failures}

    def _record_success(self) -> None:
        """AI 调用成功：清零连续失败计数与错误摘要（横幅自动消失）。"""
        with self._health_lock:
            self._consecutive_failures = 0
            self._last_error = ""

    def _record_failure(self, session_username: str, raw_error: str) -> None:
        """AI 分析最终失败：累计健康度并广播 analysis_error 事件。

        任何异常不得影响管线线程（共享约定 4）。
        """
        friendly = friendly_ai_error(raw_error)
        with self._health_lock:
            self._consecutive_failures += 1
            self._last_error = friendly
            failures = self._consecutive_failures
        try:
            self._notifier.broadcast("analysis_error", {
                "session_username": session_username,
                "error": friendly,
                "consecutive_failures": failures,
            })
        except Exception:
            logger.exception("广播 analysis_error 事件失败（已忽略）")

    # ------------------------------------------------------------------
    # 单元处理
    # ------------------------------------------------------------------
    def _process(self, unit: AnalysisUnit) -> None:
        """预筛 → AI 判定（重试）→ 落库 → 广播。"""
        # 时序诊断（三段时间 + 端到端）：定位「慢」到底发生在哪一段，
        # 避免把合并窗口/排队的等待误读成 AI 接口慢。
        t_dequeued = time.monotonic()
        start_wall = unit.first_seen_epoch or time.time()
        # 合并等待用墙钟（同一时钟源），排队等待用单调时钟（不受校时影响）
        agg_wait_ms = _elapsed_ms(unit.enqueued_epoch, unit.first_seen_epoch)
        queue_wait_ms = _elapsed_ms(t_dequeued, unit.enqueued_mono)
        analysis_cfg = dbmod.read_analysis_settings(self._db)
        base_log = dict(
            session_username=unit.session_username,
            sender_wxid=unit.sender_wxid,
            msg_count=unit.merged_count,
            input_text=unit.merged_text()[:2000],
            agg_wait_ms=agg_wait_ms,
            queue_wait_ms=queue_wait_ms,
        )
        # 不使用 AI 接口（设置里可关闭）：直接用「轻量筛选」关键词/正则判定商机。
        # 该模式不向外部发送消息内容，因此不要求合规知情确认。
        ai_cfg = dbmod.read_ai_settings(self._db)
        if not self._ai_enabled(ai_cfg):
            self._process_prescreen_only(unit, analysis_cfg, base_log, start_wall)
            return
        # 合规未确认：不启动分析（共享约定/待明确事项 4）
        if not analysis_cfg.get("compliance_confirmed"):
            base_log["e2e_ms"] = _elapsed_ms(time.time(), start_wall)
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
                base_log["e2e_ms"] = _elapsed_ms(time.time(), start_wall)
                self._db.insert_analysis_log(status="skipped",
                                             error="预筛未命中", model="",
                                             **base_log)
                return
        # AI 配置检查
        api_key = decrypt_api_key(ai_cfg.get("api_key_enc", ""))
        if not ai_cfg.get("base_url") or not api_key or not ai_cfg.get("model"):
            err = "AI 接口未配置完整，请在设置页配置"
            base_log["e2e_ms"] = _elapsed_ms(time.time(), start_wall)
            self._db.insert_analysis_log(status="error", error=err,
                                         model="", **base_log)
            self._record_failure(unit.session_username, err)
            return
        client = self._build_client(ai_cfg, api_key)
        # 发起分析：记录「收到消息 → 发起 AI」的分段耗时（用户可据此判断
        # 是窗口/排队等待，还是 AI 接口本身慢）。thinking 后括号内是实际
        # 生效的参数形态（thinking/enable_thinking/reasoning_effort），
        # 换模型后据此确认关闭思考是否仍然生效。
        thinking_state = "off" if getattr(client, "disable_thinking", False) else "on"
        variant = getattr(client, "_thinking_variant", None)
        logger.info(
            "[分析发起] %s/%s 合并 %d 条 | 收消息→入队 %.2fs（合并等待）"
            " | 入队→出队 %.2fs（排队）| 模型=%s thinking=%s%s",
            unit.session_name, unit.sender_name, unit.merged_count,
            (agg_wait_ms or 0) / 1000.0, (queue_wait_ms or 0) / 1000.0,
            ai_cfg.get("model", ""), thinking_state,
            "(%s)" % variant if variant else "")

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
                    examples=analysis_cfg.get("correction_examples") or [],
                )
                self._record_success()
                latency_ms = int((time.monotonic() - started) * 1000)
                base_log["e2e_ms"] = _elapsed_ms(time.time(), start_wall)
                logger.info(
                    "[分析返回] %s/%s AI %.2fs（%d token）| 端到端 %.2fs"
                    " | %s",
                    unit.session_name, unit.sender_name, latency_ms / 1000.0,
                    int(result.get("completion_tokens") or 0),
                    (base_log["e2e_ms"] or 0) / 1000.0,
                    ("命中 %s，置信度 %s" % (result.get("type"),
                                           result.get("confidence")))
                    if result.get("hit") else "未命中")
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
        # 最终失败：落库 + 健康度累计 + WS 广播（优化 10）
        base_log["e2e_ms"] = _elapsed_ms(time.time(), start_wall)
        self._db.insert_analysis_log(status="error", error=last_err,
                                     model=ai_cfg.get("model", ""), **base_log)
        self._record_failure(unit.session_username, last_err)

    @staticmethod
    def _ai_enabled(ai_cfg: dict) -> bool:
        """设置里的「是否使用 AI 接口」（"0"/"false"/"no"/"off" 视为关闭）。"""
        raw = ai_cfg.get("enabled", "1")
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() not in ("0", "false", "no", "off", "")

    def _process_prescreen_only(self, unit: AnalysisUnit, analysis_cfg: dict,
                                base_log: dict, start_wall: float) -> None:
        """不使用 AI 接口：仅用「轻量筛选」关键词/正则判定商机。

        语义（用户口径）：命中任一关键词或正则即算商机（confidence=100），
        未命中则记 skipped；未配置任何规则时无法判定，同样记 skipped 并告警。
        判定结果与 AI 结果同构，复用 ``_on_result`` 的合并/落库/广播链路。
        """
        unit_text = unit.merged_text()
        keywords = str(analysis_cfg.get("prescreen_keywords") or "")
        regex = str(analysis_cfg.get("prescreen_regex") or "")
        started = time.monotonic()
        if not keywords.strip() and not regex.strip():
            base_log["e2e_ms"] = _elapsed_ms(time.time(), start_wall)
            self._db.insert_analysis_log(
                status="skipped", model="轻量筛选",
                error="未使用 AI 且未配置「轻量筛选」关键词/正则，无法判定商机",
                **base_log)
            logger.warning("未使用 AI 且未配置轻量筛选规则，分析单元被跳过："
                           "%s/%s", unit.session_name, unit.sender_name)
            return
        hits = analyzer.prescreen_hits(unit_text, keywords, regex)
        if not hits:
            base_log["e2e_ms"] = _elapsed_ms(time.time(), start_wall)
            self._db.insert_analysis_log(
                status="skipped", model="轻量筛选",
                error="轻量筛选未命中（未使用 AI 接口）", **base_log)
            return
        quote = ""
        for _hm, text in unit.lines:
            first = str(text or "").strip().split("\n", 1)[0].strip()
            if first:
                quote = first[:120]
                break
        result = {
            "hit": True,
            "type": "轻量筛选命中",
            "points": ["命中：" + h for h in hits[:3]],
            "confidence": 100,
            "quote": quote,
            "direction": "",
            "prompt_tokens": None,
            "completion_tokens": None,
        }
        latency_ms = int((time.monotonic() - started) * 1000)
        base_log["e2e_ms"] = _elapsed_ms(time.time(), start_wall)
        logger.info("[轻量筛选] %s/%s 命中 %s（未使用 AI，未调用接口）",
                    unit.session_name, unit.sender_name, "、".join(hits[:3]))
        self._on_result(unit, result, latency_ms, base_log, model="轻量筛选")

    @staticmethod
    def _thinking_flag(raw: object, default: bool = True) -> bool:
        """解析 disable_thinking 配置（"0"/"false"/"no"/"off" 视为关闭）。"""
        if raw is None or raw == "":
            return default
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() not in ("0", "false", "no", "off", "")

    def _build_client(self, ai_cfg: dict, api_key: str):
        """按配置构建 AI 客户端。

        disable_thinking 随配置下发（默认关闭思考模式）：思考型模型默认
        每条消息多生成 700~1600 个隐藏推理 token，实测把 3 秒级响应拖到
        27~60 秒；判定任务只需固定 JSON，无推理收益。
        """
        disable_thinking = self._thinking_flag(ai_cfg.get("disable_thinking"))
        try:
            return self._client_factory({
                "base_url": ai_cfg.get("base_url", ""),
                "api_key": api_key,
                "model": ai_cfg.get("model", ""),
                "timeout_sec": int(float(ai_cfg.get("timeout_sec", "60"))),
                "temperature": float(ai_cfg.get("temperature", "0.2")),
                "disable_thinking": disable_thinking,
            })
        except (TypeError, ValueError):
            return self._client_factory({
                "base_url": ai_cfg.get("base_url", ""),
                "api_key": api_key,
                "model": ai_cfg.get("model", ""),
                "timeout_sec": 60,
                "temperature": 0.2,
                "disable_thinking": disable_thinking,
            })

    @staticmethod
    def _default_client_factory(cfg: dict):
        """默认 AIClient 工厂。"""
        return analyzer.AIClient(
            base_url=cfg["base_url"], api_key=cfg["api_key"],
            model=cfg["model"], timeout_sec=cfg["timeout_sec"],
            temperature=cfg["temperature"],
            disable_thinking=bool(cfg.get("disable_thinking", True)),
        )

    # ------------------------------------------------------------------
    # 结果处理
    # ------------------------------------------------------------------
    def _on_result(self, unit: AnalysisUnit, result: dict, latency_ms: int,
                   base_log: dict, model: str) -> None:
        """按命中/门槛/未命中分流落库与广播；命中先尝试商机级合并。

        token 用量（反馈 13）随日志落库；direction（反馈 4）随商机落库。
        """
        result_json = json.dumps(result, ensure_ascii=False)
        usage = {
            "prompt_tokens": result.get("prompt_tokens"),
            "completion_tokens": result.get("completion_tokens"),
        }
        if not result.get("hit"):
            self._db.insert_analysis_log(
                status="ok", result=result_json, model=model,
                latency_ms=latency_ms, **base_log, **usage)
            return
        analysis_cfg = dbmod.read_analysis_settings(self._db)
        min_conf = int(analysis_cfg.get("min_confidence", 60))
        if result["confidence"] < min_conf:
            self._db.insert_analysis_log(
                status="below_threshold", result=result_json, model=model,
                latency_ms=latency_ms, **base_log, **usage)
            return
        # 命中类型：AI 返回类型名 → 匹配已启用类型拿 type_id（找不到则存快照）
        type_name = result.get("type") or "未知类型"
        type_id: Optional[int] = None
        for t in dbmod.iter_enabled_types(self._db):
            if t.get("name") == type_name:
                type_id = int(t["id"])
                break
        # 商机合并窗口内同发送人的 pending 商机 → 合并；否则新建
        existing = self._find_mergeable(analysis_cfg, unit)
        if existing is not None:
            self._merge_into(existing, unit, result, type_id, type_name,
                             base_log, model, latency_ms)
            return
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
            direction=str(result.get("direction") or ""),
        )
        self._db.insert_analysis_log(
            status="ok", result=result_json, model=model,
            latency_ms=latency_ms, opportunity_id=opp["id"],
            prompt_tokens=result.get("prompt_tokens"),
            completion_tokens=result.get("completion_tokens"),
            **base_log)
        self._notifier.mark_new_opportunity(opp["id"])
        self._notifier.broadcast("opportunity_new", opp)
        logger.info("新商机 #%d：%s/%s（%s，置信度 %d）",
                    opp["id"], unit.session_name, unit.sender_name,
                    type_name, result["confidence"])

    # ------------------------------------------------------------------
    # 商机合并（第二级窗口：analysis.opp_merge_window_sec，默认 180 秒）
    # ------------------------------------------------------------------
    def _find_mergeable(self, analysis_cfg: dict,
                        unit: AnalysisUnit) -> Optional[dict]:
        """查找同一会话+发送人、窗口内且仍 pending 的既有商机。

        O-1 时间跨度上限：目标商机 window_start 距今超过
        ``config.MAX_OPPORTUNITY_SPAN_SEC``（默认 1800 秒）时不再续命，
        返回 None 走新建，杜绝长期滚动合并导致单条商机无限膨胀。
        """
        merge_window = int(analysis_cfg.get(
            "opp_merge_window_sec", OPP_MERGE_WINDOW_SEC_DEFAULT))
        cutoff = (datetime.now() - timedelta(seconds=merge_window)).isoformat(
            timespec="seconds")
        try:
            existing = self._db.find_mergeable_opportunity(
                session_username=unit.session_username,
                sender_wxid=unit.sender_wxid,
                sender_name=unit.sender_name,
                min_window_end_iso=cutoff,
            )
        except Exception:
            logger.exception("查找可合并商机失败（按新建处理）")
            return None
        if existing is None:
            return None
        try:
            started = datetime.fromisoformat(
                str(existing.get("window_start") or ""))
            span_sec = (datetime.now() - started).total_seconds()
            if span_sec > MAX_OPPORTUNITY_SPAN_SEC:
                logger.info(
                    "商机 #%d 时间跨度 %.0f 秒已超上限 %d 秒，不再合并（新建）",
                    existing["id"], span_sec, MAX_OPPORTUNITY_SPAN_SEC)
                return None
        except (ValueError, TypeError):
            pass  # window_start 格式异常时不做跨度拦截，维持原合并行为
        return existing

    def _merge_into(self, existing: dict, unit: AnalysisUnit, result: dict,
                    type_id: Optional[int], type_name: str, base_log: dict,
                    model: str, latency_ms: int) -> None:
        """把本批次命中合并进既有商机并广播 opportunity_update。

        合并规则（BUG-1/BUG-2 修复后为原子操作，见 db.merge_opportunity）：
        raw_text 追加新批次文本（SQL 端拼接，保持"[HH:MM] 内容"行格式）、
        window_end 更新为新批次末时间、merged_count SQL 端累加、summary
        追加快照中不存在的新要点（并集去重）、key_quote 取最新、
        confidence 取两者较大值（SQL 端 MAX）。status 已非 pending 时
        UPDATE 不生效，走新建兜底，新命中内容不被吞。
        """
        # O-3：新批次未识别出类型时保留原商机的类型快照，避免被"未知类型"覆盖
        if not str(result.get("type") or "").strip():
            type_id = existing.get("type_id")
            type_name = str(existing.get("type_name") or type_name)
        # 追加要点：仅取快照 summary 中不存在的（并集去重；拼接由 SQL 原子完成）
        old_points = {p.strip() for p in
                      str(existing.get("summary") or "").split("\n")}
        old_points.discard("")
        missing: list[str] = []
        for p in result.get("points", []):
            p = str(p).strip()
            if p and p not in old_points and p not in missing:
                missing.append(p)
        opp = self._db.merge_opportunity(
            existing["id"],
            raw_text_append=unit.merged_text(),
            summary_append="\n".join(missing),
            window_end=unit.window_end,
            merged_count_delta=unit.merged_count,
            key_quote=str(result.get("quote") or existing.get("key_quote", "")),
            confidence=int(result["confidence"]),
            type_id=type_id,
            type_name=type_name,
        )
        if opp is None:  # 已回复/已忽略/已删除/并发状态变化：退回新建
            logger.warning("合并目标商机 #%d 已非 pending，改为新建商机",
                           existing["id"])
            self._on_result_new_fallback(unit, result, type_id, type_name,
                                         base_log, model, latency_ms)
            return
        self._db.insert_analysis_log(
            status="ok", result=json.dumps(result, ensure_ascii=False),
            model=model, latency_ms=latency_ms,
            opportunity_id=opp["id"],
            prompt_tokens=result.get("prompt_tokens"),
            completion_tokens=result.get("completion_tokens"),
            **self._base_log_with_defaults(unit, base_log))
        self._notifier.broadcast("opportunity_update", opp)
        logger.info("商机 #%d 已合并新批次：%s/%s（累计 %d 条，置信度 %d）",
                    opp["id"], unit.session_name, unit.sender_name,
                    opp["merged_count"], opp["confidence"])

    @staticmethod
    def _base_log_with_defaults(unit: AnalysisUnit,
                                base_log: dict) -> dict:
        """以 unit 字段补齐 base_log 缺失的必填键（防御外部直接调用）。"""
        log = {
            "session_username": unit.session_username,
            "sender_wxid": unit.sender_wxid,
            "msg_count": unit.merged_count,
            "input_text": unit.merged_text()[:2000],
        }
        log.update(base_log or {})
        return log

    def _on_result_new_fallback(self, unit: AnalysisUnit, result: dict,
                                type_id: Optional[int], type_name: str,
                                base_log: dict, model: str,
                                latency_ms: int) -> None:
        """合并目标丢失时的新建兜底（逻辑与新建分支一致）。"""
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
            direction=str(result.get("direction") or ""),
        )
        self._db.insert_analysis_log(
            status="ok", result=json.dumps(result, ensure_ascii=False),
            model=model, latency_ms=latency_ms,
            opportunity_id=opp["id"],
            prompt_tokens=result.get("prompt_tokens"),
            completion_tokens=result.get("completion_tokens"),
            **self._base_log_with_defaults(unit, base_log))
        self._notifier.mark_new_opportunity(opp["id"])
        self._notifier.broadcast("opportunity_new", opp)
