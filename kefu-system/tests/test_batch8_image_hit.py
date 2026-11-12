# -*- coding: utf-8 -*-
"""批次 8：图片已识别为车辆/配件却判不上商机的修复。

问题现场（2026-11-12 用户反馈 + 实机复现）：

- 视觉模型把图识别成「车辆/汽车配件（前保险杠）」，分析日志却是「AI 未命中」；
- 根因不是商机类型描述，而是管线 ``_apply_image_direction`` **只在 AI 已命中
  时**覆盖 direction。文本模型看不到图，面对的只是一句
  「[图片] 识别：车辆/汽车配件（前保险杠）」——既无意向措辞也无数量价格，
  按 prompt「只基于消息文本判断、不要臆测」只能判 hit=false，视觉识别的
  token 白花（实测「车辆铭牌」稳定未命中，「前保险杠」在 60~70 分抖动）。

修复分三层，本文件覆盖其中两层（第三层是 prompt 文案，见
``TestImagePromptGuidance``）：

1. 管线兜底：``has_image_sell`` 且 AI 未命中 → 补齐 hit/type/points/quote/
   confidence，并置 ``image_forced``（视觉确认 = 有实物 = 商机）；
2. 置信度下限：AI 命中但给分低于 70 → 抬到 70，避免被 ``min_confidence``
   拦成 below_threshold 白丢商机。
"""
from __future__ import annotations

import pytest

from app import analyzer
from app.aggregator import AnalysisUnit
from app.db import Database
from app.notifier import Notifier
from app.pipeline import IMAGE_EVIDENCE_CONFIDENCE, Pipeline
from tests.test_core import FakeChatClient

USER = "12345@chatroom"
MISS = {"hit": False, "type": None, "points": [], "confidence": 0,
        "quote": "", "direction": ""}


def make_image_unit(text: str = "[图片] 识别：车辆/汽车配件（前保险杠）",
                    ) -> AnalysisUnit:
    """一个只含「图片识别结论」的群聊分析单元。"""
    return AnalysisUnit(
        session_username=USER,
        session_name="拆车件群",
        session_kind="group",
        sender_wxid="wxid_li",
        sender_name="李四",
        window_start="2026-11-12T20:22:00",
        window_end="2026-11-12T20:22:30",
        merged_count=1,
        lines=[("20:22", text)],
        local_ids=[132],
        has_image_sell=True,
    )


def build_pipeline(result: dict, type_name: str = "汽车配件") -> tuple:
    db = Database(":memory:")
    db.insert_type(type_name, "汽车配件 OE 号 / 车架号 / 配件实物照片")
    db.set_setting("ai.base_url", "https://mock.local/v1")
    db.set_setting("ai.model", "mock-model")
    db.set_setting("compliance.confirmed", "1")
    client = FakeChatClient(result=dict(result))
    pipeline = Pipeline(db, Notifier(), client_factory=lambda cfg: client)
    return db, pipeline


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr("app.pipeline.decrypt_api_key", lambda raw: "sk-test")


class TestImageForcedOpportunity:
    def test_ai_miss_becomes_opportunity(self):
        """AI 判未命中，但图已确认是配件 → 兜底产出商机。"""
        db, pipeline = build_pipeline(MISS)
        pipeline._process(make_image_unit())
        total, rows = db.list_opportunities()
        assert total == 1
        opp = rows[0]
        assert opp["type_name"] == "汽车配件"
        assert opp["direction"] == "sell"
        assert opp["confidence"] >= IMAGE_EVIDENCE_CONFIDENCE
        # 部件名进 summary；quote 是含标记的那一行原文
        assert "前保险杠" in (opp["summary"] or "")
        assert "识别：车辆/汽车配件（前保险杠）" in (opp["key_quote"] or "")

    def test_vin_carried_into_points(self):
        """识别出车架号时，车架号要进要点。"""
        db, pipeline = build_pipeline(MISS)
        pipeline._process(make_image_unit(
            "[图片] 识别：车辆/汽车配件（车辆铭牌） 车架号：LSGMJ5P578V016154"))
        _total, rows = db.list_opportunities()
        assert "LSGMJ5P578V016154" in (rows[0]["summary"] or "")

    def test_ai_hit_keeps_type_but_raises_confidence(self):
        """AI 已命中：type 不动，但低分被抬到下限（避免 below_threshold）。"""
        db, pipeline = build_pipeline({
            "hit": True, "type": "汽车配件", "points": ["前保险杠"],
            "confidence": 40, "quote": "x", "direction": "buy"})
        pipeline._process(make_image_unit())
        _total, rows = db.list_opportunities()
        assert rows[0]["type_name"] == "汽车配件"
        assert rows[0]["confidence"] == IMAGE_EVIDENCE_CONFIDENCE
        assert rows[0]["direction"] == "sell"

    def test_without_image_miss_stays_miss(self):
        """没有图片证据时，AI 判未命中依然不产商机（不能放宽整体判定）。"""
        db, pipeline = build_pipeline(MISS)
        unit = make_image_unit("[20:22] 今天天气不错")
        unit.has_image_sell = False
        pipeline._process(unit)
        assert db.list_opportunities()[0] == 0

    def test_log_marks_forced(self):
        """分析日志要能看出这条是图片兜底命中（便于排查）。"""
        db, pipeline = build_pipeline(MISS)
        pipeline._process(make_image_unit())
        import json
        _total, logs = db.list_analysis_logs()
        payload = json.loads(logs[0]["result"])
        assert payload["image_forced"] is True


class TestApplyImageDirectionUnit:
    def _unit(self) -> AnalysisUnit:
        return make_image_unit()

    def test_miss_branch_fills_all_fields(self):
        result = dict(MISS)
        Pipeline._apply_image_direction(
            self._unit(), result, [{"name": "汽车配件", "description": ""}])
        assert result["hit"] is True
        assert result["type"] == "汽车配件"
        assert result["direction"] == "sell"
        assert result["confidence"] == IMAGE_EVIDENCE_CONFIDENCE
        assert result["points"] == ["图片识别：前保险杠"]
        assert result["image_forced"] is True

    def test_hit_branch_untouched_when_high_confidence(self):
        result = {"hit": True, "type": "汽车配件", "points": ["x"],
                  "confidence": 95, "quote": "q", "direction": "sell"}
        Pipeline._apply_image_direction(self._unit(), result)
        assert result == {"hit": True, "type": "汽车配件", "points": ["x"],
                          "confidence": 95, "quote": "q", "direction": "sell"}

    def test_no_flag_is_noop(self):
        unit = make_image_unit()
        unit.has_image_sell = False
        result = dict(MISS)
        Pipeline._apply_image_direction(unit, result)
        assert result == MISS

    def test_part_name_without_parentheses(self):
        """合成文本没有括号（部件未识别）时，points 用兜底文案。"""
        result = dict(MISS)
        Pipeline._apply_image_direction(
            make_image_unit("[图片] 识别：车辆/汽车配件"), result,
            [{"name": "汽车配件"}])
        assert result["points"] == ["图片识别：车辆/汽车配件实物"]


class TestPickImageType:
    def test_prefers_part_type(self):
        types = [{"name": "机械加工订单"}, {"name": "汽车配件"}]
        assert Pipeline._pick_image_type(types) == "汽车配件"

    def test_single_type_fallback(self):
        assert Pipeline._pick_image_type([{"name": "随便一个"}]) == "随便一个"

    def test_ambiguous_returns_empty(self):
        types = [{"name": "A"}, {"name": "B"}]
        assert Pipeline._pick_image_type(types) == ""

    def test_empty_list(self):
        assert Pipeline._pick_image_type([]) == ""
        assert Pipeline._pick_image_type(None) == ""

    def test_unknown_type_kept_as_opportunity(self):
        """类型挑不出来也要保住商机（存「未知类型」由用户归类）。"""
        db, pipeline = build_pipeline(MISS, type_name="杂项")
        # 「杂项」不含配件关键词，但只有一个启用类型 → 仍然能挑中
        pipeline._process(make_image_unit())
        _total, rows = db.list_opportunities()
        assert rows[0]["type_name"] == "杂项"


class TestImagePromptGuidance:
    def test_prompt_explains_image_tag(self):
        prompt = analyzer.build_system_prompt(
            [{"name": "汽车配件", "description": "配件"}])
        assert "[图片] 识别：车辆/汽车配件" in prompt
        assert "必须 hit=true" in prompt
        # 花括号必须已转义（format 没抛异常即已隐含验证，这里再断言格式段完整）
        assert "## 输出 JSON 格式" in prompt
        assert "{{" not in prompt

    def test_prompt_still_has_direction_guidance(self):
        prompt = analyzer.build_system_prompt([{"name": "汽车配件"}])
        assert "## 买卖方向判定（direction）" in prompt
        assert "15887775311" in prompt
