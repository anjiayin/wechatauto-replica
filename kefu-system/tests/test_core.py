# -*- coding: utf-8 -*-
"""核心管线单元测试：db DAO / aggregator / analyzer / pipeline / sender。

全部不依赖真实微信：wechatauto 以懒加载方式被绕过，AI 用替身客户端注入。
运行（在 kefu-system 目录下）::

    python -m pytest tests/ -v
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta
from typing import List

import pytest

from app.aggregator import Aggregator, AnalysisUnit
from app import analyzer
from app.crypto import encrypt_api_key, decrypt_api_key, mask_key
from app.db import Database
from app.notifier import Notifier
from app.pipeline import Pipeline
from app.sender import SendTask, Sender
from app.wechat_service import WeChatService


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def make_msg(content: str, sort_seq: int = 1, sender_username: str = "wxid_zhang",
             username: str = "12345@chatroom", mtype: str = "文本",
             create_time: float | None = None) -> dict:
    """构造 Listener 回调消息 dict。"""
    return {
        "local_id": sort_seq,
        "type": mtype,
        "sender_id": 3,
        "sender_username": sender_username,
        "create_time": create_time or time.time(),
        "content": content,
        "sort_seq": sort_seq,
        "username": username,
    }


def make_unit() -> AnalysisUnit:
    """构造一个标准分析单元。"""
    return AnalysisUnit(
        session_username="12345@chatroom",
        session_name="加工外协群",
        session_kind="group",
        sender_wxid="wxid_zhang",
        sender_name="张三",
        window_start="2026-11-02T14:02:00",
        window_end="2026-11-02T14:05:00",
        merged_count=2,
        lines=[("14:02", "有谁能做铝合金CNC"), ("14:05", "大概500件，急")],
    )


class FakeChatClient:
    """AIClient 替身：chat() 返回构造好的 JSON。"""

    def __init__(self, result: dict | None = None, fail_times: int = 0) -> None:
        self.result = result or {"hit": True, "type": "机械加工订单",
                                 "points": ["铝合金CNC 500件"], "confidence": 85,
                                 "quote": "有谁能做铝合金CNC"}
        self.fail_times = fail_times
        self.calls = 0

    def chat(self, system_prompt: str, user_message: str) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise analyzer.AIError("模拟调用失败")
        return json.dumps(self.result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# db DAO
# ---------------------------------------------------------------------------
class TestDatabase:
    def test_settings_roundtrip(self):
        db = Database(":memory:")
        assert db.get_setting("nonexistent", "dft") == "dft"
        assert db.get_setting("analysis.strictness") == "balanced"
        db.set_setting("analysis.strictness", "strict")
        assert db.get_setting("analysis.strictness") == "strict"
        assert db.get_bool("analysis.prescreen_enabled") is False
        db.set_setting("analysis.prescreen_enabled", "1")
        assert db.get_bool("analysis.prescreen_enabled") is True

    def test_target_crud(self):
        db = Database(":memory:")
        t = db.insert_target("12345@chatroom", "加工群", "group")
        assert t["display_name"] == "加工群"
        with pytest.raises(Exception):
            db.insert_target("12345@chatroom", "重复", "group")  # UNIQUE
        db.update_target_enabled(t["id"], False)
        assert db.get_target(t["id"])["enabled"] == 0
        assert db.list_enabled_target_usernames() == []
        db.update_target_enabled(t["id"], True)
        assert db.list_enabled_target_usernames() == ["12345@chatroom"]
        assert db.delete_target(t["id"]) == 1
        assert db.get_target(t["id"]) is None

    def test_types_crud(self):
        db = Database(":memory:")
        t = db.insert_type("机械加工订单", "寻找机械加工的采购需求")
        assert db.list_types(only_enabled=True)[0]["name"] == "机械加工订单"
        db.update_type(t["id"], {"enabled": 0})
        assert db.list_types(only_enabled=True) == []
        db.update_type(t["id"], {"description": "新描述"})
        assert db.get_type(t["id"])["description"] == "新描述"
        assert db.delete_type(t["id"]) == 1
        assert db.get_type(t["id"]) is None

    def test_opportunity_and_logs(self):
        db = Database(":memory:")
        t = db.insert_type("机械加工订单", "desc")
        opp = db.insert_opportunity(
            session_username="12345@chatroom", session_name="加工群",
            sender_wxid="wxid_zhang", sender_name="张三",
            window_start="2026-11-02T14:02:00",
            window_end="2026-11-02T14:05:00", merged_count=2,
            raw_text="[14:02] 内容", type_id=t["id"],
            type_name="机械加工订单", summary="要点", key_quote="引文",
            confidence=85)
        total, rows = db.list_opportunities()
        assert total == 1 and rows[0]["id"] == opp["id"]
        # 状态筛选：空 = 排除 ignored
        db.update_opportunity_status(opp["id"], "ignored")
        assert db.list_opportunities() == (0, [])
        assert db.list_opportunities(status="ignored")[0] == 1
        # 关键词搜索
        db.update_opportunity_status(opp["id"], "pending")
        _, kw = db.list_opportunities(keyword="铝合金")
        assert kw == []
        _, kw2 = db.list_opportunities(keyword="要点")
        assert len(kw2) == 1
        # 回复标记
        assert db.set_opportunity_replied(opp["id"], "可以做的") == 1
        assert db.get_opportunity(opp["id"])["status"] == "replied"
        # 日志
        log_id = db.insert_analysis_log(
            session_username="12345@chatroom", msg_count=1,
            status="ok", result=json.dumps({"hit": True}))
        assert log_id > 0

    def test_watermarks(self):
        db = Database(":memory:")
        assert db.load_watermarks() == {}
        db.save_watermarks({"a@chatroom": 100, "wxid_b": 200})
        marks = db.load_watermarks()
        assert marks == {"a@chatroom": 100, "wxid_b": 200}
        db.save_watermarks({"a@chatroom": 150})
        assert db.load_watermarks()["a@chatroom"] == 150


class TestCrypto:
    def test_encrypt_decrypt_mask(self):
        cipher = encrypt_api_key("sk-abcdef123456")
        assert cipher != "sk-abcdef123456"
        assert decrypt_api_key(cipher) == "sk-abcdef123456"
        masked = mask_key(cipher)
        assert masked.startswith("sk-a") and "****" in masked
        assert mask_key("") == ""
        # 无效密文回退空串
        assert decrypt_api_key("gAAAA-invalid") == ""


# ---------------------------------------------------------------------------
# aggregator
# ---------------------------------------------------------------------------
class TestAggregator:
    def test_merge_and_flush(self):
        flushed: List[AnalysisUnit] = []
        agg = Aggregator(flush_callback=flushed.append,
                         window_sec_provider=lambda: 300)
        t = time.time()
        assert agg.add(make_msg("第一条", 1, create_time=t), "加工群", "group") is True
        assert agg.add(make_msg("第二条", 2, create_time=t + 5), "加工群", "group") is True
        # 窗口未满不 flush
        agg._flush_expired()
        assert flushed == []
        # 强制 flush：同会话同人合并为一个单元
        agg._flush_expired(force=True)
        assert len(flushed) == 1
        unit = flushed[0]
        assert unit.merged_count == 2
        assert unit.session_kind == "group"
        assert unit.sender_name == "wxid_zhang"
        assert unit.merged_text().startswith("[") and "第一条" in unit.merged_text()

    def test_different_senders_split(self):
        flushed: List[AnalysisUnit] = []
        agg = Aggregator(flush_callback=flushed.append,
                         window_sec_provider=lambda: 300)
        agg.add(make_msg("甲的话", 1, sender_username="wxid_a"), "群", "group")
        agg.add(make_msg("乙的话", 2, sender_username="wxid_b"), "群", "group")
        agg._flush_expired(force=True)
        assert len(flushed) == 2
        names = {u.sender_wxid for u in flushed}
        assert names == {"wxid_a", "wxid_b"}

    def test_empty_content_rejected(self):
        agg = Aggregator(flush_callback=lambda u: None,
                         window_sec_provider=lambda: 300)
        assert agg.add(make_msg("  "), "群", "group") is False

    def test_scheduler_thread(self):
        flushed: List[AnalysisUnit] = []
        agg = Aggregator(flush_callback=flushed.append,
                         window_sec_provider=lambda: 1)
        agg.start()
        agg.add(make_msg("很快过期", 1), "群", "group")
        deadline = time.time() + 12
        while not flushed and time.time() < deadline:
            time.sleep(0.2)
        agg.stop(flush_all=False)
        assert len(flushed) == 1

    def test_micro_window_flush_timing(self):
        """优化 1：微窗口到期即 flush，最大延迟 ≈ 窗口 + 1s 扫描间隔（不再等 5 分钟）。

        注：_flush_expired 内有 max(5, window) 安全下限，测试用 5s 窗口验证时序。
        """
        flushed: List[AnalysisUnit] = []
        agg = Aggregator(flush_callback=flushed.append,
                         window_sec_provider=lambda: 5)
        started = time.time()
        agg.start()
        agg.add(make_msg("微窗口消息", 1), "群", "group")
        deadline = time.time() + 12  # 窗口 5s + 调度 1s，留足余量
        while not flushed and time.time() < deadline:
            time.sleep(0.05)
        elapsed = time.time() - started
        agg.stop(flush_all=False)
        assert len(flushed) == 1
        # 静默窗口 5s + 扫描间隔 1s 内完成（远小于原 5 分钟）
        assert elapsed < 8.0

    def test_force_flush_safety_valve(self):
        """O-2 安全阀：首条消息年龄超过 微窗口×10 时无条件 flush，
        杜绝持续发言导致的分析延迟无上界与缓冲无上限。"""
        flushed: List[AnalysisUnit] = []
        agg = Aggregator(flush_callback=flushed.append,
                         window_sec_provider=lambda: 5)
        now = time.time()
        # 首条 60s 前入缓冲（年龄 60 > 5×10=50），最后一条 1s 前（静默 < 窗口）
        agg.add(make_msg("开头", 1, create_time=now - 60), "群", "group")
        agg.add(make_msg("最近", 2, create_time=now - 1), "群", "group")
        agg._flush_expired()
        assert len(flushed) == 1  # 静默不足但首条年龄超限 → 强制 flush
        assert "开头" in flushed[0].merged_text()
        # 对照：首条年龄未超限时，持续发言不触发强制 flush
        flushed2: List[AnalysisUnit] = []
        agg2 = Aggregator(flush_callback=flushed2.append,
                          window_sec_provider=lambda: 5)
        agg2.add(make_msg("a", 1, create_time=now - 3), "群", "group")
        agg2.add(make_msg("b", 2, create_time=now - 1), "群", "group")
        agg2._flush_expired()
        assert flushed2 == []


# ---------------------------------------------------------------------------
# analyzer
# ---------------------------------------------------------------------------
class TestAnalyzer:
    def test_system_prompt_injection(self):
        types = [{"name": "机械加工订单", "description": "寻找加工的采购需求"}]
        prompt = analyzer.build_system_prompt(types, "strict")
        assert "【机械加工订单】寻找加工的采购需求" in prompt
        assert "严格(strict)" in prompt
        assert "宽松(lenient)" not in prompt  # 只注入当前档位细则
        # 空类型：一律 hit=false
        empty_prompt = analyzer.build_system_prompt([], "balanced")
        assert "一律 hit=false" in empty_prompt

    def test_parse_ai_json(self):
        ok = analyzer.parse_ai_json(
            '{"hit": true, "type": "加工", "points": ["a"], '
            '"confidence": 85, "quote": "原文"}')
        assert ok == {"hit": True, "type": "加工", "points": ["a"],
                      "confidence": 85, "quote": "原文", "direction": ""}
        # markdown 包裹 + 正则兜底
        ok2 = analyzer.parse_ai_json('```json\n{"hit": false, "type": null, '
                                     '"points": [], "confidence": 10}\n```')
        assert ok2["hit"] is False and ok2["type"] is None
        # 置信度越界收敛
        ok3 = analyzer.parse_ai_json('{"hit": false, "confidence": 999}')
        assert ok3["confidence"] == 100
        with pytest.raises(analyzer.AIError):
            analyzer.parse_ai_json("完全不是 JSON")
        with pytest.raises(analyzer.AIError):
            analyzer.parse_ai_json('{"hit": true, "type": null}')  # 命中却无类型

    def test_prescreen(self):
        assert analyzer.prescreen("有采购需求", "采购,求购", "") is True
        assert analyzer.prescreen("今天天气不错", "采购,求购", "") is False
        assert analyzer.prescreen("找加工厂", "", "加[工工]") is True
        assert analyzer.prescreen("随便聊聊", "", "") is True  # 空 = 全通过

    def test_prescreen_chinese_comma(self):
        """体验优化 2：中文逗号（，）可正确分隔关键词，不再并入单个关键词。"""
        assert analyzer.prescreen("有采购需求", "采购，求购", "") is True
        assert analyzer.prescreen("今天天气不错", "采购，求购", "") is False
        # 混合中英文逗号
        assert analyzer.prescreen("求购铝锭", "采购,求购，报价", "") is True
        assert analyzer.prescreen("随便聊聊", "采购,求购，报价", "") is False
        # 中文逗号分隔的每个关键词独立生效（修复前 "采购，求购" 整串匹配）
        assert analyzer.prescreen("有求购需求", "采购，求购", "") is True

    def test_system_prompt_correction_examples(self):
        """体验优化 6：修正示例注入 Prompt（段落 + 逐条 + 100 字截断 + 上限 20）。"""
        types = [{"name": "配件采购", "description": "寻找配件采购需求"}]
        examples = ["求丰田42431-0K090原厂件，要50个"]
        prompt = analyzer.build_system_prompt(types, "balanced",
                                              examples=examples)
        assert "用户确认过的真实商机消息示例" in prompt
        assert "42431-0K090" in prompt
        # 无示例：不注入段落
        prompt2 = analyzer.build_system_prompt(types, "balanced")
        assert "商机消息示例" not in prompt2
        # 每条截断 100 字
        long_ex = "长" * 150
        prompt3 = analyzer.build_system_prompt(types, "balanced",
                                               examples=[long_ex])
        assert "长" * 100 in prompt3
        assert "长" * 101 not in prompt3
        # 上限 20 条
        prompt4 = analyzer.build_system_prompt(
            types, "balanced", examples=[f"示例{i}" for i in range(30)])
        assert "示例19" in prompt4 and "示例20" not in prompt4
        # 空白条目被过滤
        prompt5 = analyzer.build_system_prompt(types, "balanced",
                                               examples=["  ", "有效示例"])
        assert "有效示例" in prompt5

    def test_summarize_points(self):
        assert analyzer.summarize_points(["a", "b"]) == "a\nb"
        assert analyzer.summarize_points([]) == ""


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------
class TestPipeline:
    def _prepare(self):
        db = Database(":memory:")
        notifier = Notifier()
        db.insert_type("机械加工订单", "desc")
        # AI 配置（密文 Key）+ 合规确认
        db.set_setting("ai.base_url", "https://mock.local/v1")
        db.set_setting("ai.api_key_enc", encrypt_api_key("sk-test-12345678"))
        db.set_setting("ai.model", "mock-model")
        db.set_setting("compliance.confirmed", "1")
        db.set_setting("analysis.min_confidence", "60")
        return db, notifier

    def test_hit_creates_opportunity(self):
        db, notifier = self._prepare()
        client = FakeChatClient()
        pipeline = Pipeline(db, notifier,
                            client_factory=lambda cfg: client)
        pipeline._process(make_unit())
        total, rows = db.list_opportunities()
        assert total == 1
        opp = rows[0]
        assert opp["type_name"] == "机械加工订单"
        assert opp["sender_name"] == "张三"
        assert "铝合金CNC" in opp["summary"]
        # 日志 ok 且带 opportunity_id
        assert client.calls == 1

    def test_below_threshold(self):
        db, notifier = self._prepare()
        client = FakeChatClient(result={"hit": True, "type": "机械加工订单",
                                        "points": ["x"], "confidence": 30,
                                        "quote": "q"})
        pipeline = Pipeline(db, notifier,
                            client_factory=lambda cfg: client)
        pipeline._process(make_unit())
        assert db.list_opportunities() == (0, [])  # 不生成商机

    def test_retry_then_success(self):
        db, notifier = self._prepare()
        client = FakeChatClient(fail_times=1)  # 失败 1 次 → 重试成功
        pipeline = Pipeline(db, notifier,
                            client_factory=lambda cfg: client)
        pipeline._process(make_unit())
        assert client.calls == 2
        assert db.list_opportunities()[0] == 1

    def test_prescreen_skips(self):
        db, notifier = self._prepare()
        db.set_setting("analysis.prescreen_enabled", "1")
        db.set_setting("analysis.prescreen_keywords", "采购,求购")
        client = FakeChatClient()
        pipeline = Pipeline(db, notifier,
                            client_factory=lambda cfg: client)
        # 单元文本无"采购/求购" → 预筛跳过，不调 AI
        pipeline._process(make_unit())
        assert client.calls == 0
        assert db.list_opportunities() == (0, [])

    def test_correction_examples_injected_into_prompt(self):
        """体验优化 6：管线把 analysis.correction_examples 注入系统提示词。"""
        captured: dict = {}

        class CaptureClient:
            def chat(self, system_prompt: str, user_message: str) -> str:
                captured["prompt"] = system_prompt
                return json.dumps({"hit": False, "type": None, "points": [],
                                   "confidence": 10, "quote": ""})

        db, notifier = self._prepare()
        db.set_setting("analysis.correction_examples",
                       json.dumps(["求丰田42431-0K090原厂件"],
                                  ensure_ascii=False))
        pipeline = Pipeline(db, notifier,
                            client_factory=lambda cfg: CaptureClient())
        pipeline._process(make_unit())
        assert "用户确认过的真实商机消息示例" in captured["prompt"]
        assert "42431-0K090" in captured["prompt"]

    def test_not_configured(self):
        db, notifier = self._prepare()
        db.set_setting("ai.base_url", "")
        client = FakeChatClient()
        pipeline = Pipeline(db, notifier,
                            client_factory=lambda cfg: client)
        pipeline._process(make_unit())
        assert client.calls == 0

    def test_not_configured_records_health_failure(self):
        """优化 10：AI 未配置也计入健康度失败（用户可感知）。"""
        db, notifier = self._prepare()
        db.set_setting("ai.base_url", "")
        pipeline = Pipeline(db, notifier,
                            client_factory=lambda cfg: FakeChatClient())
        pipeline._process(make_unit())
        health = pipeline.analysis_health
        assert health["consecutive_failures"] == 1
        assert "未配置" in health["last_error"]

    def test_compliance_gate(self):
        db, notifier = self._prepare()
        db.set_setting("compliance.confirmed", "0")
        client = FakeChatClient()
        pipeline = Pipeline(db, notifier,
                            client_factory=lambda cfg: client)
        pipeline._process(make_unit())
        assert client.calls == 0

    def test_queue_overflow_counts(self):
        db, notifier = self._prepare()
        pipeline = Pipeline(db, notifier, queue_max=1,
                            client_factory=lambda cfg: FakeChatClient())
        assert pipeline.enqueue(make_unit()) is True
        assert pipeline.enqueue(make_unit()) is False
        assert pipeline.dropped_total == 1


# ---------------------------------------------------------------------------
# AI 分析健康度 + analysis_error 广播（优化 10）
# ---------------------------------------------------------------------------
class TestAnalysisHealth:
    def _prepare(self):
        db = Database(":memory:")
        notifier = Notifier()
        db.insert_type("机械加工订单", "desc")
        db.set_setting("ai.base_url", "https://mock.local/v1")
        db.set_setting("ai.api_key_enc", encrypt_api_key("sk-test-12345678"))
        db.set_setting("ai.model", "mock-model")
        db.set_setting("compliance.confirmed", "1")
        return db, notifier

    def test_final_failure_records_health_and_broadcasts(self):
        """重试耗尽后：健康度累计 + 广播 analysis_error（payload 含会话/摘要/次数）。"""
        from app.config import RETRY_TIMES
        db, notifier = self._prepare()
        events: List[tuple] = []
        notifier.broadcast = lambda event, data: events.append((event, data))
        client = FakeChatClient(fail_times=10)
        pipeline = Pipeline(db, notifier, client_factory=lambda cfg: client)
        pipeline._process(make_unit())
        assert client.calls == 1 + RETRY_TIMES  # 首调 + 重试 2 次
        health = pipeline.analysis_health
        assert health["consecutive_failures"] == 1
        assert "模拟调用失败" in health["last_error"]
        err_events = [e for e in events if e[0] == "analysis_error"]
        assert len(err_events) == 1
        payload = err_events[0][1]
        assert payload["session_username"] == "12345@chatroom"
        assert payload["consecutive_failures"] == 1
        assert "模拟调用失败" in payload["error"]

    def test_success_resets_health(self):
        """AI 恢复成功：健康度清零（前端常驻横幅随之消失）。"""
        db, notifier = self._prepare()
        notifier.broadcast = lambda event, data: None
        client = FakeChatClient(fail_times=10)
        pipeline = Pipeline(db, notifier, client_factory=lambda cfg: client)
        pipeline._process(make_unit())
        assert pipeline.analysis_health["consecutive_failures"] == 1
        client.fail_times = 0  # 恢复
        pipeline._process(make_unit())
        assert pipeline.analysis_health == {"last_error": "",
                                            "consecutive_failures": 0}
        total, _ = db.list_opportunities()
        assert total == 1

    def test_consecutive_failures_accumulate(self):
        """连续多批失败：计数累加，广播 payload 携带累计次数。"""
        db, notifier = self._prepare()
        payloads: List[dict] = []
        notifier.broadcast = lambda event, data: (
            payloads.append(data) if event == "analysis_error" else None)
        client = FakeChatClient(fail_times=99)
        pipeline = Pipeline(db, notifier, client_factory=lambda cfg: client)
        pipeline._process(make_unit())
        pipeline._process(make_unit())
        assert pipeline.analysis_health["consecutive_failures"] == 2
        assert payloads[-1]["consecutive_failures"] == 2

    def test_friendly_error_mapping(self):
        """错误摘要友好映射：401/403 → Key 问题；429 → 额度；超时；其余前 80 字。"""
        from app.pipeline import friendly_ai_error
        assert friendly_ai_error(
            "AI 接口调用失败：Error code: 401 - Invalid API key") == "Key 无效或无权限"
        assert friendly_ai_error(
            "AI 接口调用失败：Error code: 403 - permission denied") == "Key 无效或无权限"
        assert friendly_ai_error(
            "AI 接口调用失败：Error code: 429 - rate limit exceeded") == "额度不足或触发限流"
        assert friendly_ai_error("Request timed out.") == "接口超时"
        assert friendly_ai_error("连接超时（60 秒）") == "接口超时"
        raw = "AI 接口调用失败：" + "x" * 200
        assert friendly_ai_error(raw) == ("AI 接口调用失败：" + "x" * 200)[:80]
        assert friendly_ai_error("") == "未知错误"


# ---------------------------------------------------------------------------
# 预筛内置预设正则（优化 4：前端 constants/prescreen.ts 的两串预设
# 在后端 re 引擎必须可用且可命中，此处逐字复制防止漂移）
# ---------------------------------------------------------------------------
OE_PRESET = (r"(?<![A-Za-z0-9])(?:\d{4,5}-[A-Z0-9]{3,7}(?:-[A-Z0-9]{2,4})?"
             r"|[A-Z0-9]{4}-\d{7}-[A-Za-z]"
             r"|[A-Z]{1,3}[- ]?\d{2,3}[- ]?\d{3}[- ]?\d{1,3}(?:[- ]?\d{1,3})?"
             r"|\d{10,13}|(?i:(?=[A-Z0-9]*[A-Z])[A-Z0-9]{8,15}))"
             r"(?![A-Za-z0-9])")
VIN_PRESET = r"(?<![A-Za-z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Za-z0-9])"


class TestPrescreenRegexPresets:
    def test_oe_preset_matches(self):
        import re
        assert re.search(OE_PRESET, "求丰田42431-0K090原厂件")
        assert re.search(OE_PRESET, "本田04465-SZA-A020到货了吗")
        assert re.search(OE_PRESET, "_digits 13800138000")  # 纯数字 10~13 位
        assert re.search(OE_PRESET, "A12345678 报价")  # 字母开头 8~15 位（OE 预设范围）
        assert not re.search(OE_PRESET, "LFV3A23C8K3123456")  # 17 位归 VIN 预设

    def test_oe_preset_no_false_positive(self):
        import re
        assert not re.search(OE_PRESET, "今天天气不错聊聊家常")

    def test_vin_preset_matches_and_excludes_ioq(self):
        import re
        assert re.search(VIN_PRESET, "VIN码LFV3A23C8K3123456出售")
        assert not re.search(VIN_PRESET, "VIN码LFV3A23C8K31234I6")  # 含 I 不合法

    def test_merged_regex_still_works_in_prescreen(self):
        """前端 `|` 合并后的存储串可直接用于后端预筛（命中任一通过）。"""
        merged = f"{OE_PRESET}|{VIN_PRESET}"
        assert analyzer.prescreen("求42431-0K090", "", merged) is True
        assert analyzer.prescreen("车架号LFV3A23C8K3123456", "", merged) is True
        assert analyzer.prescreen("随便聊聊", "", merged) is False


# ---------------------------------------------------------------------------
# 商机合并窗口（优化 1 第二级：默认 180 秒）
# ---------------------------------------------------------------------------
def recent_unit(lines: List[tuple] | None = None,
                window_end_offset_sec: float = 0.0,
                session: str = "12345@chatroom",
                sender_wxid: str = "wxid_zhang",
                sender_name: str = "张三") -> AnalysisUnit:
    """构造 window_end 为「现在 + offset」的分析单元（时间可自由控制）。"""
    end_epoch = time.time() + window_end_offset_sec
    start_epoch = end_epoch - 60
    fmt = lambda e: datetime.fromtimestamp(e).isoformat(timespec="seconds")
    hm = lambda e: datetime.fromtimestamp(e).strftime("%H:%M")
    ls = lines or [(hm(start_epoch), "有谁能做铝合金CNC")]
    return AnalysisUnit(
        session_username=session,
        session_name="加工外协群",
        session_kind="group",
        sender_wxid=sender_wxid,
        sender_name=sender_name,
        window_start=fmt(start_epoch),
        window_end=fmt(end_epoch),
        merged_count=len(ls),
        lines=ls,
    )


class TestOpportunityMerge:
    """同发送人商机合并 / 超时新建 / 已回复与已忽略新建。"""

    def _prepare(self):
        db = Database(":memory:")
        notifier = Notifier()
        db.insert_type("机械加工订单", "desc")
        db.set_setting("ai.base_url", "https://mock.local/v1")
        db.set_setting("ai.api_key_enc", encrypt_api_key("sk-test-12345678"))
        db.set_setting("ai.model", "mock-model")
        db.set_setting("compliance.confirmed", "1")
        db.set_setting("analysis.min_confidence", "60")
        return db, notifier

    def _events(self, notifier) -> List[str]:
        events: List[str] = []
        notifier.broadcast = lambda event, data: events.append(event)
        return events

    def test_second_hit_merges_into_existing(self):
        """180s 内同发送人再次命中 → 合并为一条 + opportunity_update。"""
        db, notifier = self._prepare()
        events = self._events(notifier)
        client = FakeChatClient(result={
            "hit": True, "type": "机械加工订单",
            "points": ["追加要点：交期两周"], "confidence": 90,
            "quote": "急，两周内要"})
        pipeline = Pipeline(db, notifier, client_factory=lambda cfg: client)
        pipeline._process(recent_unit([("14:02", "有谁能做铝合金CNC")],
                                      window_end_offset_sec=-120))
        total, rows = db.list_opportunities()
        assert total == 1
        first_id = rows[0]["id"]
        assert events == ["opportunity_new"]
        # 第二批命中（window_end 距今 10 秒，在 180s 合并窗口内）
        pipeline._process(recent_unit([("14:05", "急，两周内要")],
                                      window_end_offset_sec=-10))
        total, rows = db.list_opportunities()
        assert total == 1  # 未新建
        opp = db.get_opportunity(first_id)
        assert opp["merged_count"] == 2
        assert "有谁能做铝合金CNC" in opp["raw_text"]
        assert "急，两周内要" in opp["raw_text"]  # raw_text 追加
        assert "[14:05]" in opp["raw_text"]       # 保持 [HH:MM] 行格式
        # summary 要点并集去重（旧 + 新）
        assert "铝合金CNC 500件" not in opp["summary"]  # 第一批 client 未用此 result
        assert "追加要点：交期两周" in opp["summary"]
        # confidence 取较大值（90 > 85）
        assert opp["confidence"] == 90
        # key_quote 取最新
        assert opp["key_quote"] == "急，两周内要"
        assert opp["status"] == "pending"
        # analysis_logs 关联到被合并商机
        assert events[-1] == "opportunity_update"
        log = db._conn.execute(
            "SELECT opportunity_id FROM analysis_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert log["opportunity_id"] == first_id

    def test_merge_points_dedup_union(self):
        """要点并集去重：旧要点与新要点重复项只保留一份。"""
        db, notifier = self._prepare()
        self._events(notifier)
        client = FakeChatClient(result={
            "hit": True, "type": "机械加工订单",
            "points": ["需要加工铝合金件", "数量约500件"], "confidence": 85,
            "quote": "q1"})
        pipeline = Pipeline(db, notifier, client_factory=lambda cfg: client)
        pipeline._process(recent_unit([("14:02", "铝合金500件")],
                                      window_end_offset_sec=-60))
        # 第二批：其中一条要点重复
        client.result = {"hit": True, "type": "机械加工订单",
                         "points": ["数量约500件", "交期两周"], "confidence": 80,
                         "quote": "q2"}
        pipeline._process(recent_unit([("14:05", "两周内交货")],
                                      window_end_offset_sec=-10))
        total, rows = db.list_opportunities()
        assert total == 1
        summary_lines = rows[0]["summary"].split("\n")
        assert sorted(summary_lines) == sorted(
            ["需要加工铝合金件", "数量约500件", "交期两周"])

    def test_merge_window_timeout_creates_new(self):
        """超过商机合并窗口（180s）→ 不合并，新建商机。"""
        db, notifier = self._prepare()
        events = self._events(notifier)
        client = FakeChatClient()
        pipeline = Pipeline(db, notifier, client_factory=lambda cfg: client)
        # 第一批 window_end 距今 10 分钟（远超 180s 窗口）
        pipeline._process(recent_unit([("13:00", "第一批")],
                                      window_end_offset_sec=-600))
        pipeline._process(recent_unit([("14:05", "第二批")]))
        total, rows = db.list_opportunities()
        assert total == 2
        assert events.count("opportunity_new") == 2
        assert "opportunity_update" not in events

    def test_replied_opportunity_creates_new(self):
        """商机合并窗口内的商机若已回复 → 不合并，新建。"""
        db, notifier = self._prepare()
        self._events(notifier)
        client = FakeChatClient()
        pipeline = Pipeline(db, notifier, client_factory=lambda cfg: client)
        pipeline._process(recent_unit([("14:02", "第一批")],
                                      window_end_offset_sec=-30))
        total, rows = db.list_opportunities()
        db.set_opportunity_replied(rows[0]["id"], "已回复")
        pipeline._process(recent_unit([("14:05", "第二批")]))
        total, rows = db.list_opportunities()
        assert total == 2
        assert all(o["status"] in ("pending", "replied") for o in rows)

    def test_ignored_opportunity_creates_new(self):
        """商机合并窗口内的商机若已忽略 → 不合并，新建。"""
        db, notifier = self._prepare()
        self._events(notifier)
        client = FakeChatClient()
        pipeline = Pipeline(db, notifier, client_factory=lambda cfg: client)
        pipeline._process(recent_unit([("14:02", "第一批")],
                                      window_end_offset_sec=-30))
        _, rows = db.list_opportunities()
        db.update_opportunity_status(rows[0]["id"], "ignored")
        pipeline._process(recent_unit([("14:05", "第二批")]))
        total, _ = db.list_opportunities()
        assert total == 1  # ignored 默认不在列表；新建 1 条 pending
        assert db.list_opportunities(status="ignored")[0] == 1

    def test_different_sender_no_merge(self):
        """同会话但不同发送人 → 不合并，各自新建。"""
        db, notifier = self._prepare()
        self._events(notifier)
        client = FakeChatClient()
        pipeline = Pipeline(db, notifier, client_factory=lambda cfg: client)
        pipeline._process(recent_unit([("14:02", "张三的话")],
                                      window_end_offset_sec=-30))
        pipeline._process(recent_unit([("14:05", "李四的话")],
                                      window_end_offset_sec=-20,
                                      sender_wxid="wxid_li",
                                      sender_name="李四"))
        total, _ = db.list_opportunities()
        assert total == 2

    def test_sender_wxid_empty_falls_back_to_name(self):
        """wxid 缺失时按昵称匹配合并（共享约定 9.2）。"""
        db, notifier = self._prepare()
        self._events(notifier)
        client = FakeChatClient()
        pipeline = Pipeline(db, notifier, client_factory=lambda cfg: client)
        pipeline._process(recent_unit([("14:02", "第一批")],
                                      window_end_offset_sec=-30,
                                      sender_wxid=""))
        pipeline._process(recent_unit([("14:05", "第二批")],
                                      window_end_offset_sec=-10,
                                      sender_wxid=""))
        total, rows = db.list_opportunities()
        assert total == 1
        assert db.get_opportunity(rows[0]["id"])["merged_count"] == 2

    def test_opp_merge_window_setting_respected(self):
        """analysis.opp_merge_window_sec 可覆盖默认 180s。"""
        db, notifier = self._prepare()
        self._events(notifier)
        db.set_setting("analysis.opp_merge_window_sec", "10")
        client = FakeChatClient()
        pipeline = Pipeline(db, notifier, client_factory=lambda cfg: client)
        # window_end 距今 30s：默认 180s 会合并，但窗口缩到 10s 后应新建
        pipeline._process(recent_unit([("14:02", "第一批")],
                                      window_end_offset_sec=-30))
        pipeline._process(recent_unit([("14:05", "第二批")]))
        total, _ = db.list_opportunities()
        assert total == 2

    def test_merge_span_cap_creates_new(self):
        """O-1：商机 window_start 距今超过 30 分钟跨度上限 → 不再续命，新建。"""
        from app.config import MAX_OPPORTUNITY_SPAN_SEC
        db, notifier = self._prepare()
        self._events(notifier)
        client = FakeChatClient()
        pipeline = Pipeline(db, notifier, client_factory=lambda cfg: client)
        # 直接构造一条长期滚动合并的商机：起点远超跨度上限、window_end 近期
        start_iso = (datetime.now() - timedelta(
            seconds=MAX_OPPORTUNITY_SPAN_SEC + 200)).isoformat(timespec="seconds")
        end_iso = (datetime.now() - timedelta(seconds=5)).isoformat(
            timespec="seconds")
        db.insert_opportunity(
            session_username="12345@chatroom", session_name="加工外协群",
            sender_wxid="wxid_zhang", sender_name="张三",
            window_start=start_iso, window_end=end_iso, merged_count=5,
            raw_text="[09:00] 历史文本", type_id=None,
            type_name="机械加工订单", summary="旧要点", key_quote="",
            confidence=85)
        # 新批次 window_end 距今 1s（在 180s 合并窗口内），但起点超限 → 新建
        pipeline._process(recent_unit([("14:05", "新一批")],
                                      window_end_offset_sec=-1))
        total, rows = db.list_opportunities()
        assert total == 2
        new_opp = db.get_opportunity(max(rows, key=lambda r: r["id"])["id"])
        assert new_opp["merged_count"] == 1  # 新建而非并入 merged_count=5 的旧条

    def test_merge_within_span_cap_still_merges(self):
        """O-1 对照：起点在跨度上限内的商机照常合并。"""
        db, notifier = self._prepare()
        self._events(notifier)
        client = FakeChatClient()
        pipeline = Pipeline(db, notifier, client_factory=lambda cfg: client)
        pipeline._process(recent_unit([("14:02", "第一批")],
                                      window_end_offset_sec=-30))
        pipeline._process(recent_unit([("14:05", "第二批")],
                                      window_end_offset_sec=-10))
        total, _ = db.list_opportunities()
        assert total == 1


# ---------------------------------------------------------------------------
# sender（GUI 用替身注入）
# ---------------------------------------------------------------------------
class FakeWxResponse(dict):
    """WxResponse 替身（dict 子类 + is_success 属性，同 wechatauto.param）。"""

    def __init__(self, ok: bool, message: str):
        super().__init__(status="成功" if ok else "失败", message=message)
        self.is_success = ok


class FakeGUI:
    """WeChatGUI 替身（三重校验路径可配置）。"""

    def __init__(self, alive=True, visible=True, open_ok=True, send_ok=True):
        self.alive = alive
        self.visible = visible
        self.open_ok = open_ok
        self.send_ok = send_ok
        self.sent: List[str] = []

    def is_alive(self):
        return self.alive

    def bring_to_front(self, keep_topmost=False):
        return True

    def ensure_visible(self):
        return self.visible

    def open_chat(self, name, exact=False):
        return self.open_ok

    def send_msg(self, text, who=None, verify=False):
        self.sent.append(text)
        return FakeWxResponse(self.send_ok,
                              "消息已发送并确认" if self.send_ok else "发送失败：模拟")


class TestSender:
    def _prepare(self):
        db = Database(":memory:")
        db.insert_type("机械加工订单", "desc")
        opp = db.insert_opportunity(
            session_username="12345@chatroom", session_name="加工群",
            sender_wxid="wxid_zhang", sender_name="张三",
            window_start="2026-11-02T14:02:00",
            window_end="2026-11-02T14:05:00", merged_count=1,
            raw_text="原文", type_id=1, type_name="机械加工订单",
            summary="要点", key_quote="引文", confidence=85)
        sender = Sender(db, Notifier())
        return db, opp, sender

    def test_send_success_marks_replied(self):
        db, opp, sender = self._prepare()
        gui = FakeGUI()
        sender._ensure_gui = lambda: gui  # 注入替身
        sender._process(SendTask(opportunity_id=opp["id"],
                                 session_username="12345@chatroom",
                                 session_name="加工群", text="您好，可以做"))
        row = db.get_opportunity(opp["id"])
        assert row["status"] == "replied"
        assert row["reply_content"] == "您好，可以做"
        assert gui.sent == ["您好，可以做"]

    def test_window_invisible_fails(self):
        db, opp, sender = self._prepare()
        gui = FakeGUI(visible=False)
        sender._ensure_gui = lambda: gui
        sender._process(SendTask(opportunity_id=opp["id"],
                                 session_username="12345@chatroom",
                                 session_name="加工群", text="hi"))
        assert db.get_opportunity(opp["id"])["status"] == "pending"
        assert gui.sent == []

    def test_open_chat_failure(self):
        db, opp, sender = self._prepare()
        gui = FakeGUI(open_ok=False)
        sender._ensure_gui = lambda: gui
        sender._process(SendTask(opportunity_id=opp["id"],
                                 session_username="12345@chatroom",
                                 session_name="不存在的群", text="hi"))
        assert db.get_opportunity(opp["id"])["status"] == "pending"

    def test_send_failure_keeps_pending(self):
        db, opp, sender = self._prepare()
        gui = FakeGUI(send_ok=False)
        sender._ensure_gui = lambda: gui
        sender._process(SendTask(opportunity_id=opp["id"],
                                 session_username="12345@chatroom",
                                 session_name="加工群", text="hi"))
        assert db.get_opportunity(opp["id"])["status"] == "pending"


# ---------------------------------------------------------------------------
# wechat_service（不触碰真实微信的路径）
# ---------------------------------------------------------------------------
class TestWeChatService:
    def test_degraded_state(self):
        db = Database(":memory:")
        agg = Aggregator(flush_callback=lambda u: None)
        svc = WeChatService(db, agg)
        # 未 start：默认降级态且不崩溃
        assert svc.listener_running is False
        assert svc.probe_wechat_window() in (True, False)

    def test_display_name_fallback(self):
        db = Database(":memory:")
        svc = WeChatService(db, Aggregator(lambda u: None))
        # wechat 组件不可用时回退 username 本身
        assert svc.get_display_name("12345@chatroom") == "12345@chatroom"

    def test_on_message_non_text_skips(self):
        db = Database(":memory:")
        got: List[dict] = []
        agg = Aggregator(lambda u: got.append(u))
        svc = WeChatService(db, agg)
        svc._on_message(make_msg("[图片]", mtype="图片"))
        assert got == []  # 非文本不入缓冲


# ---------------------------------------------------------------------------
# 脱离微信的端到端小闭环（回调 → 合并 → 管线）
# ---------------------------------------------------------------------------
class TestMiniPipeline:
    def test_callback_to_opportunity(self):
        db = Database(":memory:")
        db.insert_type("机械加工订单", "寻找加工的采购需求")
        db.set_setting("compliance.confirmed", "1")
        db.set_setting("ai.base_url", "https://mock.local/v1")
        db.set_setting("ai.api_key_enc", encrypt_api_key("sk-test-12345678"))
        db.set_setting("ai.model", "mock-model")
        notifier = Notifier()
        pipeline = Pipeline(db, notifier,
                            client_factory=lambda cfg: FakeChatClient())
        events: List[dict] = []
        agg = Aggregator(flush_callback=lambda u: pipeline._process(u))
        notifier.broadcast = lambda event, data: events.append(event)
        svc = WeChatService(db, agg)
        svc._on_message(make_msg("有谁能做铝合金CNC", 1))
        svc._on_message(make_msg("大概500件", 2))
        agg._flush_expired(force=True)
        deadline = time.time() + 5
        while db.count_pending_opportunities() == 0 and time.time() < deadline:
            time.sleep(0.1)
        assert db.count_pending_opportunities() == 1
        assert "opportunity_new" in events


# ---------------------------------------------------------------------------
# 体验优化 1：会话搜索 TTL 缓存（全量列表 + 内存过滤）
# ---------------------------------------------------------------------------
class FakeWxDb:
    """WeChatDB 替身：记录调用次数，模拟冷启动慢查询与瞬时故障。"""

    def __init__(self) -> None:
        self.sessions_calls = 0
        self.contacts_calls = 0
        self.msg_calls = 0
        self.msg_fail_times = 0
        self.sessions = [{"username": "111@chatroom"},
                         {"username": "gh_official"},
                         {"username": "wxid_b"}]
        self.contacts = {
            "chatroom": [{"username": "222@chatroom", "nick_name": "群二"}],
            "friend": [{"username": "wxid_c", "remark": "王五"},
                       {"username": "wxid_b", "nick_name": "李四"}],
        }
        self.msgs = [{"local_id": 1, "type": "文本",
                      "sender_username": "wxid_a",
                      "create_time": 1700000000, "content": "hello",
                      "sort_seq": 10}]

    def get_sessions(self, limit: int = 100) -> list[dict]:
        self.sessions_calls += 1
        return self.sessions

    def get_contacts(self, kind: str = "friend",
                     limit: int = 100) -> list[dict]:
        self.contacts_calls += 1
        return self.contacts.get(kind, [])

    def get_nickname(self, username: str) -> str:
        return {"111@chatroom": "加工群"}.get(username, "")

    def get_messages(self, username: str, limit: int = 50) -> list[dict]:
        self.msg_calls += 1
        if self.msg_calls <= self.msg_fail_times:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return list(self.msgs)


def make_svc() -> WeChatService:
    db = Database(":memory:")
    svc = WeChatService(db, Aggregator(lambda u: None))
    svc._wxdb = FakeWxDb()  # 注入替身（不触碰真实微信）
    return svc


class TestSessionCache:
    def test_second_search_hits_cache(self):
        """首次构建缓存后，60 秒内的搜索不再触碰微信库（第二次进入 <1s）。"""
        svc = make_svc()
        assert svc.search_sessions(keyword="") != []
        first = svc._wxdb
        assert first.sessions_calls == 1
        # 第二次搜索（换关键词/类型）→ 全部走缓存
        svc.search_sessions(keyword="王五")
        svc.search_sessions(kind="group")
        assert first.sessions_calls == 1
        assert first.contacts_calls == 2  # chatroom + friend 各一次

    def test_kind_and_keyword_filter_on_cached_list(self):
        svc = make_svc()
        groups = svc.search_sessions(kind="group")
        assert [i["username"] for i in groups] == ["111@chatroom", "222@chatroom"]
        friends = svc.search_sessions(kind="friend")
        assert [i["username"] for i in friends] == ["wxid_b", "wxid_c"]
        # 关键词命中显示名（remark 快照）
        hits = svc.search_sessions(keyword="王五")
        assert [i["username"] for i in hits] == ["wxid_c"]
        # 关键词命中 username
        hits = svc.search_sessions(keyword="222@")
        assert [i["username"] for i in hits] == ["222@chatroom"]
        # 公众号 gh_ 不出现在结果里
        all_names = [i["username"] for i in svc.search_sessions()]
        assert not any(u.startswith("gh_") for u in all_names)

    def test_ttl_expiry_rebuilds(self):
        svc = make_svc()
        svc.search_sessions()
        first = svc._wxdb
        # 未过期：走缓存
        svc.search_sessions()
        assert first.sessions_calls == 1
        # 手动把缓存时间戳拨回 61 秒前 → 过期重建
        svc._contact_cache_ts -= 61
        svc.search_sessions()
        assert first.sessions_calls == 2

    def test_build_failure_falls_back_to_stale_cache(self):
        svc = make_svc()
        svc.search_sessions()
        stale = list(svc._contact_cache)
        # 构建抛异常 → 回退过期缓存，不抛错
        def boom() -> list[dict]:
            raise RuntimeError("微信库忙")
        svc._build_contact_list = boom  # type: ignore[method-assign]
        svc._contact_cache_ts -= 61
        result = svc.search_sessions()
        assert result == stale


# ---------------------------------------------------------------------------
# 体验优化 5：微信消息查询瞬时故障重试
# ---------------------------------------------------------------------------
class TestMessageRetry:
    def test_retry_on_exception_then_success(self, monkeypatch):
        """malformed 瞬时失败 → 1.5s 后重试成功（上下文场景）。"""
        monkeypatch.setattr("app.wechat_service.time.sleep", lambda s: None)
        svc = make_svc()
        svc._wxdb.msg_fail_times = 1
        msgs, error = svc.get_messages_with_error("111@chatroom",
                                                  retry_on_empty=True)
        assert error == ""
        assert len(msgs) == 1
        assert svc._wxdb.msg_calls == 2  # 首次失败 + 1 次重试

    def test_retry_exhausted_returns_busy_error(self, monkeypatch):
        """持续异常 → 重试 2 次后返回用户可读中文 + 空列表。"""
        monkeypatch.setattr("app.wechat_service.time.sleep", lambda s: None)
        svc = make_svc()
        svc._wxdb.msg_fail_times = 99
        msgs, error = svc.get_messages_with_error("111@chatroom",
                                                  retry_on_empty=True)
        assert msgs == []
        assert "微信数据库正忙" in error
        assert "重新加载" in error
        assert svc._wxdb.msg_calls == 3  # 1 + MSG_QUERY_RETRY_TIMES

    def test_empty_with_retry_then_empty_is_genuinely_empty(self, monkeypatch):
        """空结果重试耗尽仍为空 → 按「会话确实无消息」处理（error 为空）。"""
        monkeypatch.setattr("app.wechat_service.time.sleep", lambda s: None)
        svc = make_svc()
        svc._wxdb.msgs = []  # 会话真的没消息
        msgs, error = svc.get_messages_with_error("empty@chatroom",
                                                  retry_on_empty=True)
        assert msgs == [] and error == ""
        assert svc._wxdb.msg_calls == 3  # 空结果也重试了 2 次

    def test_empty_without_retry_no_delay(self, monkeypatch):
        """默认路径（消息浏览页）：空结果不重试，立即返回。"""
        monkeypatch.setattr("app.wechat_service.time.sleep", lambda s: None)
        svc = make_svc()
        svc._wxdb.msgs = []
        msgs, error = svc.get_messages_with_error("empty@chatroom")
        assert msgs == [] and error == ""
        assert svc._wxdb.msg_calls == 1

    def test_get_messages_delegates_and_swallows_error(self, monkeypatch):
        """旧接口 get_messages：失败重试耗尽后返回空列表（不抛异常）。"""
        monkeypatch.setattr("app.wechat_service.time.sleep", lambda s: None)
        svc = make_svc()
        svc._wxdb.msg_fail_times = 99
        assert svc.get_messages("111@chatroom") == []
        # 正常路径返回消息
        svc._wxdb.msg_fail_times = 0
        svc._wxdb.msg_calls = 0
        assert len(svc.get_messages("111@chatroom")) == 1
