# -*- coding: utf-8 -*-
"""AI 延迟专项测试：思考模式开关、网关降级、时序三段埋点。

背景（实测）：GLM-5.x 等思考型模型默认把隐藏推理 token 计入 completion，
一条只需 40 token 的判定消息实际生成 700~1600 token，导致单条分析从
3 秒级放大到 27~60 秒且波动极大。本文件锁定四件事：
1. 默认请求带 thinking=disabled（降级路径不得报错）；
2. 换模型后参数形态自动切换（thinking / enable_thinking / reasoning_effort）；
3. 网关拒绝时逐个形态降级，结论记入进程缓存，不会每条消息重复试探；
4. 管线把「合并等待 / 排队 / AI / 端到端」四段耗时落库并写日志。
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from app import analyzer
from app.aggregator import Aggregator, AnalysisUnit
from app.crypto import encrypt_api_key
from app.db import Database
from app.notifier import Notifier
from app.pipeline import Pipeline, _elapsed_ms


# ---------------------------------------------------------------------------
# 替身：openai 客户端（记录请求参数，可模拟网关拒绝 thinking 参数）
# ---------------------------------------------------------------------------
class _FakeCompletions:
    def __init__(self, reject_extra_param: bool = False,
                 error: str = "", reject_keys: tuple = ()) -> None:
        self.calls: list[dict] = []
        self.reject_extra_param = reject_extra_param
        self.error = error
        # 只拒绝指定形态（模拟「认 thinking 不认 enable_thinking」之类的网关）
        self.reject_keys = set(reject_keys)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise RuntimeError(self.error)
        body = kwargs.get("extra_body") or {}
        if self.reject_keys and set(body) & self.reject_keys:
            bad = sorted(set(body) & self.reject_keys)[0]
            raise ValueError(
                f"400 Bad Request: unknown field '{bad}' in extra_body")
        if self.reject_extra_param and "extra_body" in kwargs:
            raise ValueError(
                "400 Bad Request: unknown field 'thinking' in extra_body")
        payload = {"hit": False, "type": None, "points": [], "confidence": 0,
                   "quote": "", "direction": ""}
        message = SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
        usage = SimpleNamespace(prompt_tokens=11, completion_tokens=4)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)],
                               usage=usage)


def _fake_client(**kwargs) -> SimpleNamespace:
    completions = _FakeCompletions(**kwargs)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def _make_ai_client(fake, **overrides) -> analyzer.AIClient:
    cfg = dict(base_url="https://example.invalid/v1", api_key="sk-test",
               model="glm-test", timeout_sec=30, temperature=0.2, client=fake)
    cfg.update(overrides)
    return analyzer.AIClient(**cfg)


@pytest.fixture(autouse=True)
def _clear_thinking_cache():
    """每个用例前清空进程级形态记忆，避免用例互相污染。"""
    analyzer.reset_thinking_cache()
    yield
    analyzer.reset_thinking_cache()


# ---------------------------------------------------------------------------
# 思考模式
# ---------------------------------------------------------------------------
class TestThinkingMode:
    def test_disabled_by_default(self):
        """默认请求体带 thinking=disabled（这是 27s→3s 的关键）。"""
        fake = _fake_client()
        client = _make_ai_client(fake)
        client.chat("sys", "user")
        assert fake.chat.completions.calls[0]["extra_body"] == {
            "thinking": {"type": "disabled"}}

    def test_can_be_switched_off(self):
        """配置关闭该项时不发送该参数（回到网关默认行为）。"""
        fake = _fake_client()
        client = _make_ai_client(fake, disable_thinking=False)
        client.chat("sys", "user")
        assert "extra_body" not in fake.chat.completions.calls[0]

    def test_gateway_rejects_all_variants_degrades_to_plain(self):
        """三种形态全被拒：逐个试完退回默认模式，结论进进程缓存。"""
        fake = _fake_client(reject_extra_param=True)
        client = _make_ai_client(fake)
        text = client.chat("sys", "user")
        assert "hit" in text
        calls = fake.chat.completions.calls
        assert [c.get("extra_body") for c in calls] == [
            {"thinking": {"type": "disabled"}},
            {"enable_thinking": False},
            {"reasoning_effort": "none"},
            None,
        ]
        assert client._thinking_param_ok is False
        assert client.thinking_label() == "不支持（已降级）"
        # 管线每条消息都会新建客户端：同模型/同网关不再重复试探
        fake2 = _fake_client(reject_extra_param=True)
        _make_ai_client(fake2).chat("sys", "user")
        assert len(fake2.chat.completions.calls) == 1
        assert "extra_body" not in fake2.chat.completions.calls[0]

    def test_unrelated_error_is_not_retried(self):
        """401 之类的错误不得被误当成「参数不支持」而重发。"""
        fake = _fake_client(error="401 Unauthorized: invalid api key")
        client = _make_ai_client(fake)
        with pytest.raises(analyzer.AIError):
            client.chat("sys", "user")
        assert len(fake.chat.completions.calls) == 1

    def test_test_connection_also_disables_thinking(self):
        """测试连接与正式分析口径一致，否则测出的延迟不可比。"""
        fake = _fake_client()
        client = _make_ai_client(fake)
        ok, msg, _latency = client.test_connection()
        assert ok is True
        assert fake.chat.completions.calls[0]["extra_body"] == {
            "thinking": {"type": "disabled"}}
        assert fake.chat.completions.calls[0]["max_tokens"] == 8
        # 消息里带出实际生效的形态，便于换模型后确认
        assert "thinking" in msg


# ---------------------------------------------------------------------------
# 换模型后的形态自适应（不绑死 GLM）
# ---------------------------------------------------------------------------
class TestThinkingVariantMapping:
    def test_provider_hint_picks_first_variant(self):
        """凭 base_url / 模型名推断首选形态。"""
        assert analyzer.thinking_variants(
            "https://open.bigmodel.cn/api/paas/v4", "glm-4.6")[0] == "thinking"
        assert analyzer.thinking_variants(
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "qwen-plus")[0] == "enable_thinking"
        assert analyzer.thinking_variants(
            "https://api.openai.com/v1", "gpt-5")[0] == "reasoning_effort"
        assert analyzer.thinking_variants(
            "https://api.anthropic.com/v1", "claude-sonnet-4")[0] == "thinking"

    def test_unknown_provider_uses_full_order(self):
        """认不出模型族时按固定顺序全量试探（首个仍是已验证的 thinking）。"""
        assert analyzer.thinking_variants(
            "https://example.invalid/v1", "mystery-model") == list(
                analyzer.THINKING_ORDER)

    def test_switching_model_switches_variant(self):
        """同一网关只换模型名，参数形态随之变化——证明不是绑死某个模型。"""
        fake = _fake_client()
        client = analyzer.AIClient(base_url="https://gateway.invalid/v1",
                                   api_key="sk", model="qwen-max", client=fake)
        client.chat("sys", "user")
        assert fake.chat.completions.calls[0]["extra_body"] == {
            "enable_thinking": False}

    def test_rejected_variant_moves_to_next(self):
        """首选形态被拒 → 自动换下一个形态，只多花一次往返。"""
        fake = _fake_client(reject_keys=("thinking",))
        client = _make_ai_client(fake)          # glm-test → 首选 thinking
        client.chat("sys", "user")
        calls = fake.chat.completions.calls
        assert len(calls) == 2
        assert calls[1]["extra_body"] == {"enable_thinking": False}
        assert client.thinking_label() == "enable_thinking"
        # 进程缓存生效：同 base_url+model 的新客户端不再试探
        fake2 = _fake_client(reject_keys=("thinking",))
        _make_ai_client(fake2).chat("sys", "user")
        assert len(fake2.chat.completions.calls) == 1
        assert fake2.chat.completions.calls[0]["extra_body"] == {
            "enable_thinking": False}

    def test_cache_is_per_model(self):
        """缓存按 (base_url, model) 隔离：换模型仍需各自确认。"""
        fake = _fake_client(reject_keys=("thinking",))
        _make_ai_client(fake).chat("sys", "user")          # glm-test 已确认
        fake2 = _fake_client(reject_keys=("thinking",))
        _make_ai_client(fake2, model="glm-other").chat("sys", "user")
        assert len(fake2.chat.completions.calls) == 2       # 重新试探一次

    def test_reset_cache_forces_reprobe(self):
        """手动重置（如用户改了网关/模型）后重新试探。"""
        _make_ai_client(_fake_client(reject_keys=("thinking",))).chat("sys", "user")
        analyzer.reset_thinking_cache()
        fake = _fake_client(reject_keys=("thinking",))
        _make_ai_client(fake).chat("sys", "user")
        assert len(fake.chat.completions.calls) == 2


# ---------------------------------------------------------------------------
# 时序埋点
# ---------------------------------------------------------------------------
def _prepare_db() -> Database:
    db = Database(":memory:")
    db.set_setting("ai.base_url", "https://example.invalid/v1")
    db.set_setting("ai.api_key_enc", encrypt_api_key("sk-test-key-123456"))
    db.set_setting("ai.model", "mock-model")
    db.set_setting("compliance.confirmed", "1")
    db.set_setting("analysis.min_confidence", "60")
    return db


def _unit_with_timing(agg_wait_sec: float, queue_wait_sec: float) -> AnalysisUnit:
    """构造带时序起点的分析单元（模拟 6 秒前收到消息、0.2 秒前入队）。"""
    now_mono = time.monotonic()
    return AnalysisUnit(
        session_username="12345@chatroom",
        session_name="加工外协群",
        session_kind="group",
        sender_wxid="wxid_zhang",
        sender_name="张三",
        window_start="2026-11-02T14:02:00",
        window_end="2026-11-02T14:05:00",
        merged_count=1,
        lines=[("14:02", "有谁能做铝合金CNC")],
        first_seen_epoch=time.time() - agg_wait_sec - queue_wait_sec,
        enqueued_epoch=time.time() - queue_wait_sec,
        enqueued_mono=now_mono - queue_wait_sec,
    )


class TestStageTiming:
    def test_elapsed_ms_handles_missing_and_negative(self):
        assert _elapsed_ms(0, 1) is None      # 起点缺失
        assert _elapsed_ms(1, 0) is None      # 终点缺失
        assert _elapsed_ms(1, 2) is None      # 时间倒挂（later 早于 earlier）
        assert _elapsed_ms(1.5, 1.0) == 500

    def test_log_row_keeps_four_segments(self):
        """落库后有合并等待/排队/端到端三段，且 latency 仍只表示 AI。"""
        db = _prepare_db()
        notifier = Notifier()
        pipeline = Pipeline(db, notifier,
                            client_factory=lambda cfg: _StaticClient())
        unit = _unit_with_timing(agg_wait_sec=6.0, queue_wait_sec=0.2)
        pipeline._process(unit)

        _total, rows = db.list_analysis_logs(page=1, page_size=5)
        row = rows[0]
        assert row["status"] == "ok"
        assert 5500 <= row["agg_wait_ms"] <= 7000
        assert 100 <= row["queue_wait_ms"] <= 500
        assert row["e2e_ms"] >= row["agg_wait_ms"]
        assert row["latency_ms"] is not None and row["latency_ms"] >= 0

    def test_timing_logged_at_start_and_return(self, caplog):
        """日志里能看到「分析发起」与「分析返回」两类事件。"""
        db = _prepare_db()
        notifier = Notifier()
        pipeline = Pipeline(db, notifier,
                            client_factory=lambda cfg: _StaticClient())
        with caplog.at_level("INFO", logger="app.pipeline"):
            pipeline._process(_unit_with_timing(6.0, 0.2))
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "[分析发起]" in text
        assert "[分析返回]" in text
        assert "thinking=off" in text

    def test_default_unit_without_timing_does_not_crash(self):
        """未带时序字段的老调用方式（first_seen_epoch=0）仍可正常分析。"""
        db = _prepare_db()
        notifier = Notifier()
        pipeline = Pipeline(db, notifier,
                            client_factory=lambda cfg: _StaticClient())
        unit = _unit_with_timing(6.0, 0.2)
        unit.first_seen_epoch = 0.0
        unit.enqueued_epoch = 0.0
        unit.enqueued_mono = 0.0
        pipeline._process(unit)
        _total, rows = db.list_analysis_logs(page=1, page_size=5)
        assert rows[0]["status"] == "ok"
        assert rows[0]["agg_wait_ms"] is None
        assert rows[0]["queue_wait_ms"] is None
        assert rows[0]["e2e_ms"] is not None  # 端到端退回「本函数开始时刻」

    def test_aggregator_stamps_first_seen(self):
        """Aggregator 在缓冲创建时打点，flush 出的单元携带该时间。"""
        captured: list[AnalysisUnit] = []
        agg = Aggregator(lambda u: captured.append(u),
                         window_sec_provider=lambda: 15)
        before = time.time()
        agg.add({"username": "1@chatroom", "content": "求购机油一箱",
                 "create_time": time.time(), "sender_username": "wxid_a"},
                "测试群", "group")
        agg._flush_expired(force=True)
        assert captured
        assert before <= captured[0].first_seen_epoch <= time.time() + 1


class _StaticClient:
    """固定返回未命中的 AI 替身（避免命中分支产生商机干扰断言）。"""

    disable_thinking = True

    def chat(self, system_prompt: str, user_message: str) -> str:
        return json.dumps({"hit": False, "type": None, "points": [],
                           "confidence": 10, "quote": "",
                           "direction": ""})


# ---------------------------------------------------------------------------
# 配置与迁移
# ---------------------------------------------------------------------------
class TestSettingsAndMigration:
    def test_disable_thinking_default_on(self):
        db = Database(":memory:")
        from app.db import read_ai_settings
        assert read_ai_settings(db)["disable_thinking"] == "1"

    def test_legacy_log_table_gets_new_columns(self, tmp_path):
        """老库缺三列时自动 ALTER 补齐（幂等）。"""
        path = tmp_path / "legacy.sqlite3"
        import sqlite3
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE analysis_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_username TEXT NOT NULL,
            sender_wxid TEXT,
            msg_count INTEGER NOT NULL,
            input_text TEXT,
            result TEXT,
            status TEXT NOT NULL,
            error TEXT,
            model TEXT,
            latency_ms INTEGER,
            opportunity_id INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.commit()
        conn.close()
        db = Database(str(path))
        cols = {r["name"] for r in db._conn.execute(
            "PRAGMA table_info(analysis_logs)").fetchall()}
        assert {"agg_wait_ms", "queue_wait_ms", "e2e_ms"} <= cols
        # 再写一条验证可用
        log_id = db.insert_analysis_log(
            session_username="s", status="ok", agg_wait_ms=100,
            queue_wait_ms=20, e2e_ms=3000, latency_ms=2800)
        assert log_id > 0
