# -*- coding: utf-8 -*-
"""任务 #1（第二轮）：客户端停机期间的历史消息不进入商机分析队列。

上一轮 ``monitor_since`` 只拦 create_time < 配置时刻的消息；停机期间微信
收到的消息 create_time 晚于配置时刻，且 sort_seq > 上次刷回的 watermark，
重启后会被 Listener 当「新消息」全部回放，把聚合/AI 队列打满（用户实测
端到端 450s）。本文件锁死两条修复：

1. 时间闸门：``_is_before_monitor_since`` 叠加「本次启动时刻」，早于启动的
   消息一律静默丢弃（配置水位仍独立严格生效，两者都要满足）；
2. 源头治本：``start()`` 把每个已启用会话的 watermark 推进到当前最新
   sort_seq，Listener 从最新位置开始，不再回放停机积压。

边界/存量：未调用 start()（_session_start_ts=0）时闸门不生效，老行为不变；
启动水位按秒级口径比较（同一秒视为启动后），配置水位严格比较（沿用既有语义）。
"""
from __future__ import annotations

import time

import wechatauto.db as wxdbmod

from app.db import Database
from app.wechat_service import WeChatService


class _SpyAgg:
    """记录进入商机合并窗口的消息（不触发真实 flush/AI）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def add(self, msg: dict, session_name: str, kind: str) -> bool:
        self.calls.append(msg)
        return True


def _msg(username: str, create_time: float, content: str = "我要一台发动机",
         mtype: str = "文本") -> dict:
    return {
        "local_id": 1,
        "type": mtype,
        "sender_id": 3,
        "sender_username": "wxid_seller",
        "create_time": create_time,
        "content": content,
        "sort_seq": 999,
        "username": username,
    }


def _svc_with_target(*, monitor_since: int = 1_700_000_000
                     ) -> tuple[Database, WeChatService, _SpyAgg]:
    db = Database(":memory:")
    db.insert_target("111@chatroom", "测试群", "group",
                     monitor_since=monitor_since)
    agg = _SpyAgg()
    svc = WeChatService(db, agg)  # type: ignore[arg-type]
    return db, svc, agg


# ==========================================================================
# 时间闸门：停机消息被挡，启动后消息正常
# ==========================================================================
class TestSessionStartGate:
    """本次启动水位兜底停机期间消息。"""

    def test_downtime_message_after_config_before_start_dropped(self):
        """停机消息（配置后、启动前）静默丢弃，不进聚合、不落 skipped 日志。"""
        db, svc, agg = _svc_with_target()
        configured = 1_700_000_000
        svc._session_start_ts = float(configured + 86_400)  # 停机一整天
        # create_time 晚于配置时刻（第 1 道闸门挡不住），早于启动时刻
        svc._on_message(_msg("111@chatroom", configured + 100))
        assert agg.calls == []
        logs = db._conn.execute(
            "SELECT COUNT(*) FROM analysis_logs").fetchone()[0]
        assert logs == 0

    def test_downtime_image_not_enqueued(self):
        """停机期间的图片也不能进视觉队列（队列打满的元凶之一）。"""
        _, svc, agg = _svc_with_target()
        configured = 1_700_000_000
        svc._session_start_ts = float(configured + 86_400)
        enqueued: list[dict] = []
        svc._enqueue_image_message = (  # type: ignore[method-assign]
            lambda msg, **kw: enqueued.append(msg))
        svc._on_message(_msg("111@chatroom", configured + 100,
                             mtype="图片", content="[图片]"))
        assert enqueued == []
        assert agg.calls == []

    def test_message_after_start_analyzed(self):
        """启动后的真新消息正常进入聚合。"""
        _, svc, agg = _svc_with_target()
        started = 1_700_086_400
        svc._session_start_ts = float(started)
        svc._on_message(_msg("111@chatroom", started + 5))
        assert len(agg.calls) == 1
        assert agg.calls[0]["create_time"] == started + 5

    def test_config_gate_still_strict(self):
        """配置水位仍严格生效：早 1 秒也算配置前历史（沿用既有语义）。"""
        _, svc, agg = _svc_with_target()
        # 不设置启动水位，单独验证配置闸门不受 1 秒容差影响
        svc._on_message(_msg("111@chatroom", 1_700_000_000 - 1))
        assert agg.calls == []

    def test_both_gates_dropped_independently(self):
        """配置水位与启动水位各自独立拦截，任一命中即丢弃。"""
        db, svc, agg = _svc_with_target()
        svc._session_start_ts = float(1_700_000_100)
        # 命中配置闸门但不命中启动闸门
        svc._on_message(_msg("111@chatroom", 1_699_999_999))
        # 命中启动闸门但不命中配置闸门（停机消息）
        svc._on_message(_msg("111@chatroom", 1_700_000_050))
        assert agg.calls == []
        assert db._conn.execute(
            "SELECT COUNT(*) FROM analysis_logs").fetchone()[0] == 0

    def test_legacy_target_without_config_since_still_gated_by_start(self):
        """存量 target（monitor_since=0）也受启动水位约束——停机消息照样不处理。"""
        db = Database(":memory:")
        db.insert_target("111@chatroom", "老群", "group")  # monitor_since=0
        agg = _SpyAgg()
        svc = WeChatService(db, agg)  # type: ignore[arg-type]
        svc._session_start_ts = 1_760_000_000.0
        svc._on_message(_msg("111@chatroom", 1_759_990_000))
        assert agg.calls == []
        svc._on_message(_msg("111@chatroom", 1_760_000_005))
        assert len(agg.calls) == 1

    def test_unconfigured_session_with_start_gate(self):
        """没有 target 的会话（直调）在启动后也按启动水位过滤。"""
        db = Database(":memory:")
        agg = _SpyAgg()
        svc = WeChatService(db, agg)  # type: ignore[arg-type]
        svc._session_start_ts = 1_760_000_000.0
        svc._on_message(_msg("111@chatroom", 1_759_990_000))
        assert agg.calls == []
        svc._on_message(_msg("111@chatroom", 1_760_000_001))
        assert len(agg.calls) == 1


# ==========================================================================
# 边界与存量兼容
# ==========================================================================
class TestBoundaryAndCompat:
    """启动水位容差 / 未启动时的老行为。"""

    def test_same_second_as_start_allowed(self):
        """create_time 恰好等于启动秒（整数）时放行，不误杀启动瞬间消息。"""
        _, svc, agg = _svc_with_target(monitor_since=1_700_000_000)
        started = 1_700_100_000
        svc._session_start_ts = float(started) + 0.7
        svc._on_message(_msg("111@chatroom", started))
        assert len(agg.calls) == 1

    def test_fractional_start_same_second_allowed(self):
        """启动水位带小数时，向下取整后的同一秒消息仍放行。"""
        _, svc, agg = _svc_with_target(monitor_since=1_700_000_000)
        started = 1_700_100_000
        svc._session_start_ts = float(started) + 0.9
        svc._on_message(_msg("111@chatroom", started))
        assert len(agg.calls) == 1

    def test_two_seconds_before_start_dropped(self):
        """超出 1 秒容差的停机消息被丢弃。"""
        _, svc, agg = _svc_with_target(monitor_since=1_700_000_000)
        started = 1_700_100_000
        svc._session_start_ts = float(started)
        svc._on_message(_msg("111@chatroom", started - 2))
        assert agg.calls == []

    def test_no_session_start_keeps_legacy_behavior(self):
        """未 start()（水位=0）时闸门完全不生效，老行为/测试直调不误伤。"""
        _, svc, agg = _svc_with_target(monitor_since=1_700_000_000)
        assert svc._session_start_ts == 0.0
        svc._on_message(_msg("111@chatroom", 1_700_000_005))
        assert len(agg.calls) == 1

    def test_millisecond_create_time_normalized_with_start_gate(self):
        """毫秒 create_time 归一后再与启动水位比较。"""
        _, svc, agg = _svc_with_target(monitor_since=1_700_000_000)
        started = 1_700_100_000
        svc._session_start_ts = float(started)
        svc._on_message(_msg("111@chatroom", (started - 100) * 1000))
        assert agg.calls == []
        svc._on_message(_msg("111@chatroom", (started + 5) * 1000))
        assert len(agg.calls) == 1

    def test_broken_session_start_value_degrades_to_allow(self):
        """启动水位被写成非法值时不拦截，避免误伤。"""
        _, svc, agg = _svc_with_target(monitor_since=1_700_000_000)
        svc._session_start_ts = "bad"  # type: ignore[assignment]
        svc._on_message(_msg("111@chatroom", 1_700_000_005))
        assert len(agg.calls) == 1


# ==========================================================================
# 源头治本：watermark 推进到最新 sort_seq
# ==========================================================================
class _FakeWxdb:
    def __init__(self, latest: dict[str, list[dict]]) -> None:
        self.latest = latest
        self.calls: list[tuple[str, int]] = []

    def get_messages(self, user: str, limit: int = 1) -> list[dict]:
        self.calls.append((user, limit))
        return list(self.latest.get(user, []))[:limit]


class _FakeDb:
    def __init__(self) -> None:
        self.marks = {"old@chatroom": 100, "gone@chatroom": 5}
        self.usernames = ["old@chatroom", "new@chatroom"]

    def load_watermarks(self) -> dict:
        return dict(self.marks)

    def list_enabled_target_usernames(self) -> list[str]:
        return list(self.usernames)

    def get_setting(self, key, default=""):
        return default


class TestStartWatermarkAdvance:
    """start() 把 watermark 推进到每个会话当前最新 sort_seq。"""

    def test_advance_to_latest_sort_seq(self):
        db = _FakeDb()
        wx = _FakeWxdb({
            "old@chatroom": [{"sort_seq": 5000}],
            "new@chatroom": [{"sort_seq": 8000}],
        })
        svc = WeChatService(db, None)  # type: ignore[arg-type]
        svc._wxdb = wx  # type: ignore[assignment]
        result = svc._advance_start_watermarks(db.marks)
        assert result["old@chatroom"] == 5000
        assert result["new@chatroom"] == 8000
        # 持久化水位原样保留（未启用会话不动）
        assert result["gone@chatroom"] == 5
        # 入参未被修改
        assert db.marks == {"old@chatroom": 100, "gone@chatroom": 5}
        assert ("old@chatroom", 1) in wx.calls

    def test_read_failure_keeps_persisted_watermark(self):
        db = _FakeDb()

        class _BrokenWx:
            def get_messages(self, user, limit=1):
                raise RuntimeError("微信数据库正忙")

        svc = WeChatService(db, None)  # type: ignore[arg-type]
        svc._wxdb = _BrokenWx()  # type: ignore[assignment]
        result = svc._advance_start_watermarks(db.marks)
        assert result["old@chatroom"] == 100  # 不写 0，否则会从头回放
        assert result["gone@chatroom"] == 5

    def test_no_messages_keeps_persisted_watermark(self):
        db = _FakeDb()
        svc = WeChatService(db, None)  # type: ignore[arg-type]
        svc._wxdb = _FakeWxdb({})  # type: ignore[assignment]
        result = svc._advance_start_watermarks(db.marks)
        assert result == db.marks

    def test_no_wxdb_returns_copy(self):
        db = _FakeDb()
        svc = WeChatService(db, None)  # type: ignore[arg-type]
        result = svc._advance_start_watermarks(db.marks)
        assert result == db.marks and result is not db.marks

    def test_start_records_session_ts_and_passes_advanced_watermark(
            self, monkeypatch, tmp_path):
        """start() 集成：启动水位已记录，Listener 拿到推进后的 watermark。"""
        created: list[object] = []

        class _FakeListener:
            def __init__(self, db, interval, watermark):
                self.wm = dict(watermark or {})
                self.registered: list[str] = []
                self.started = False
                created.append(self)

            def add_listener(self, user, callback):
                self.registered.append(user)

            def start(self):
                self.started = True

            def stop(self):
                pass

            @property
            def watermark(self):
                return dict(self.wm)

        db = _FakeDb()
        wx = _FakeWxdb({
            "old@chatroom": [{"sort_seq": 5000}],
            "new@chatroom": [{"sort_seq": 8000}],
        })
        monkeypatch.setattr(wxdbmod, "Listener", _FakeListener)
        monkeypatch.setattr(wxdbmod, "WeChatDB", lambda **kwargs: wx)

        svc = WeChatService(db, None)  # type: ignore[arg-type]
        svc.configured_cache_dir = lambda: tmp_path  # type: ignore[method-assign]
        svc._adopt_legacy_keys = lambda *a, **k: None  # type: ignore[method-assign]
        svc._voice_retry.start = lambda: None  # type: ignore[method-assign]
        svc._start_vision_workers = lambda: None  # type: ignore[method-assign]
        svc._image_retry.start = lambda: None  # type: ignore[method-assign]
        before = time.time()
        svc.start()
        after = time.time()

        assert svc._available is True
        assert before <= svc._session_start_ts <= after
        assert len(created) == 1
        listener = created[0]
        assert listener.wm["old@chatroom"] == 5000  # type: ignore[attr-defined]
        assert listener.wm["new@chatroom"] == 8000  # type: ignore[attr-defined]
        assert listener.wm["gone@chatroom"] == 5     # type: ignore[attr-defined]
        assert listener.registered == ["old@chatroom", "new@chatroom"]  # type: ignore[attr-defined]
        assert listener.started is True  # type: ignore[attr-defined]
        # 清理刷回线程（daemon，设 stop 让其自行退出）
        svc._stop.set()
