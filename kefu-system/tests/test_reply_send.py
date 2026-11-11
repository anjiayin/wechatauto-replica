# -*- coding: utf-8 -*-
"""回复发送专项测试（第四轮反馈：回复慢 / 引用失败 / @ 群成员无法选择）。

覆盖三件事：
1. 发送链路分段计时（组件初始化 / 前置窗口 / 打开会话 / 输入发送），
   成功与失败都要把耗时带给前端，用户才能判断慢在哪一段；
2. 引用回复的定位策略：最新一条 → 直接右键最新（不依赖文字）；更早的
   文本消息 → 清洗后的片段；更早且无可识别文字 → 明确报错，绝不引错；
3. @ 群成员：多个成员一次 @ 完只发一条消息（旧实现每个成员发一条），
   且群成员候选由接口提供（前端下拉选择，不用手输昵称）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import ALL_ROUTERS, deps
from app.db import Database
from app.notifier import Notifier
from app.sender import QuoteTargetUnavailable, SendTask, Sender


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------
class Resp(dict):
    """WxResponse 替身：既是 dict（取 message）又有 is_success 属性。"""

    def __init__(self, ok: bool = True, message: str = "已发送并确认"):
        super().__init__(message=message)
        self.is_success = ok


class RecordingNotifier(Notifier):
    """不依赖事件循环，把广播事件收集起来供断言。"""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, dict]] = []

    def broadcast(self, event: str, data) -> None:  # type: ignore[override]
        self.events.append((event, data))

    def of(self, event: str) -> list[dict]:
        return [d for e, d in self.events if e == event]


class FakeGui:
    """WeChatGUI 替身：记录调用顺序，可配置失败点。"""

    def __init__(self, open_ok: bool = True, send_ok: bool = True) -> None:
        self.open_ok = open_ok
        self.send_ok = send_ok
        self.calls: list[tuple] = []

    def is_alive(self) -> bool:
        return True

    def bring_to_front(self) -> None:
        self.calls.append(("front",))

    def ensure_visible(self) -> bool:
        return True

    def open_chat(self, name: str) -> bool:
        self.calls.append(("open", name))
        return self.open_ok

    def send_msg(self, text: str, who: str | None = None, verify: bool = False,
                 **kwargs):
        self.calls.append(("send_msg", text, who, verify,
                           kwargs.get("assume_open"),
                           kwargs.get("who_username")))
        return Resp(self.send_ok, "消息已发送并确认" if self.send_ok else "发送失败")

    def quote_msg(self, text: str, who: str | None = None,
                  target_text: str | None = None, verify: bool = False, **kwargs):
        self.calls.append(("quote_msg", text, target_text, verify,
                           kwargs.get("assume_open"),
                           kwargs.get("who_username")))
        return Resp(True, "引用已发送并确认")

    def at_members(self, members, text: str, who: str | None = None,
                   verify: bool = False, who_username: str | None = None):
        self.calls.append(("at_members", list(members), text, who, verify,
                           who_username))
        return Resp(True, "@成员消息已发送并确认")


class LegacyGui(FakeGui):
    """没有 at_members 的旧版 GUI（不应被调用，只验证兼容分支存在）。"""

    at_members = None  # type: ignore[assignment]


@pytest.fixture()
def sender(tmp_path):
    db = Database(str(tmp_path / "reply.sqlite3"))
    notifier = RecordingNotifier()
    s = Sender(db, notifier)
    yield SimpleNamespace(sender=s, db=db, notifier=notifier)
    db.close()


def make_task(**overrides) -> SendTask:
    kwargs = dict(
        opportunity_id=1,
        session_username="21264829018@chatroom",
        session_name="配件群",
        text="明天发货",
    )
    kwargs.update(overrides)
    return SendTask(**kwargs)


# ---------------------------------------------------------------------------
# 1. 分段计时
# ---------------------------------------------------------------------------
class TestStageTiming:
    def test_success_reports_stages(self, sender):
        gui = FakeGui()
        sender.sender._gui = gui
        sender.sender._process(make_task())

        results = sender.notifier.of("send_result")
        assert len(results) == 1
        assert results[0]["ok"] is True
        stages = results[0]["stages"]
        # 三段关键耗时都要有：前置窗口 / 打开会话 / 输入发送
        assert {"front", "open", "send"} <= set(stages)
        assert results[0]["total_ms"] >= sum(stages.values())
        # 打开会话只调用一次（旧实现 send_msg 内部会再打开一次）
        assert [c for c in gui.calls if c[0] == "open"] == [("open", "配件群")]

    def test_failure_reports_stages(self, sender):
        gui = FakeGui(open_ok=False)
        sender.sender._gui = gui
        sender.sender._process(make_task())

        results = sender.notifier.of("send_result")
        assert results and results[0]["ok"] is False
        assert "无法定位会话" in results[0]["message"]
        assert "front" in results[0]["stages"]

    def test_stage_summary_is_readable(self):
        from app.sender import _Stages
        st = _Stages()
        st.mark("init")
        st.mark("open")
        text = st.summary()
        assert "组件初始化" in text and "打开会话" in text


# ---------------------------------------------------------------------------
# 2. 引用定位
# ---------------------------------------------------------------------------
class TestQuoteTarget:
    def test_snippet_strips_sender_prefix(self):
        assert Sender._quote_snippet("张三:\n明天发货吗？") == "明天发货吗？"
        # 同一行的 wxid 前缀也要清洗（微信列表里不显示这个前缀）
        assert Sender._quote_snippet("wxid_abc: 明天发货") == "明天发货"

    def test_snippet_extracts_title_from_appmsg_xml(self):
        xml = ('wxid_a:\n<msg><appmsg><title>汽车配件批发 &gt;&gt;&gt;</title>'
               '<type>33</type></appmsg></msg>')
        out = Sender._quote_snippet(xml)
        assert out.startswith("汽车配件批发")
        assert "&gt;" not in out and "<" not in out

    def test_snippet_drops_placeholder_only(self):
        assert Sender._quote_snippet("[图片]") == ""
        assert Sender._quote_snippet("[小程序] 车链网") == ""

    def test_snippet_truncates_long_text(self):
        out = Sender._quote_snippet("机" * 100)
        assert len(out) <= 24

    def test_latest_message_quotes_without_text(self, sender):
        """引用最新一条：不传 target_text，直接右键最新消息（图片也能引用）。"""
        sender.sender._message_lookup = lambda u, i: {"content": "我要一箱机油"}
        sender.sender._latest_lookup = lambda u: 7
        task = make_task(quote_local_id=7)

        target = sender.sender._quote_target_text(task)
        assert target is None

    def test_older_text_message_returns_snippet(self, sender):
        sender.sender._message_lookup = lambda u, i: {"content": "张三:\n上周的报价单"}
        sender.sender._latest_lookup = lambda u: 99

        target = sender.sender._quote_target_text(make_task(quote_local_id=5))
        assert target == "上周的报价单"

    def test_older_placeholder_message_fails_loudly(self, sender):
        """更早的图片/小程序消息无法定位 → 明确报错，不默默引错消息。"""
        sender.sender._message_lookup = lambda u, i: {"content": "[图片]"}
        sender.sender._latest_lookup = lambda u: 99

        with pytest.raises(QuoteTargetUnavailable) as exc:
            sender.sender._quote_target_text(make_task(quote_local_id=5))
        assert "引用最新一条消息" in str(exc.value)

    def test_process_reports_quote_failure_as_send_failure(self, sender):
        gui = FakeGui()
        sender.sender._gui = gui
        sender.sender._message_lookup = lambda u, i: {"content": "[图片]"}
        sender.sender._latest_lookup = lambda u: 99
        sender.sender._process(make_task(quote_local_id=5))

        results = sender.notifier.of("send_result")
        assert results[0]["ok"] is False
        assert "引用最新一条消息" in results[0]["message"]
        assert not [c for c in gui.calls if c[0] == "quote_msg"]


# ---------------------------------------------------------------------------
# 3. @ 群成员
# ---------------------------------------------------------------------------
class TestAtMembers:
    def test_multi_members_sent_in_one_message(self, sender):
        gui = FakeGui()
        sender.sender._gui = gui
        sender.sender._process(make_task(at=["张三", "李四"], text="明天发货"))

        calls = [c for c in gui.calls if c[0] == "at_members"]
        assert len(calls) == 1
        assert calls[0][1] == ["张三", "李四"]
        assert calls[0][2] == "明天发货"
        assert calls[0][4] is True  # verify
        # DB 回读用会话 wxid 直查（不按显示名搜索，同名联系人不歧义）
        assert calls[0][5] == "21264829018@chatroom"

    def test_blank_member_names_ignored(self, sender):
        gui = FakeGui()
        sender.sender._gui = gui
        sender.sender._process(make_task(at=["  ", "张三"], text="在吗"))

        calls = [c for c in gui.calls if c[0] == "at_members"]
        assert calls[0][1] == ["张三"]

    def test_plain_text_does_not_call_at(self, sender):
        gui = FakeGui()
        sender.sender._gui = gui
        sender.sender._process(make_task())

        assert not [c for c in gui.calls if c[0] == "at_members"]
        sends = [c for c in gui.calls if c[0] == "send_msg"]
        # 会话刚由本流程打开：assume_open=True，避免 gui 内部再搜一次会话
        assert sends and sends[0][4] is True
        # DB 回读用会话 wxid 直查（who_username 透传不丢）
        assert sends[0][5] == "21264829018@chatroom"

    def test_quote_msg_passes_session_username(self, sender):
        """引用回复：quote_msg 收到 who_username（DB 回读按 wxid 直查）。"""
        gui = FakeGui()
        sender.sender._gui = gui
        sender.sender._message_lookup = lambda u, i: {"content": "张三:\n上周的报价单"}
        sender.sender._latest_lookup = lambda u: 99
        sender.sender._process(make_task(quote_local_id=5))

        quotes = [c for c in gui.calls if c[0] == "quote_msg"]
        assert len(quotes) == 1
        assert quotes[0][2] == "上周的报价单"      # target_text
        assert quotes[0][5] == "21264829018@chatroom"


# ---------------------------------------------------------------------------
# 4. 群成员候选接口
# ---------------------------------------------------------------------------
class FakeSvc:
    available = True
    listener_running = True
    monitored_count = 1
    watermark_saved_at = ""
    degraded_reason = ""
    ai_enabled = True
    ai_configured = True

    def __init__(self, members=None, error: Exception | None = None) -> None:
        self._members = members or []
        self._error = error

    def list_group_members(self, chatroom: str):
        if self._error:
            raise self._error
        return self._members

    def session_exists(self, username: str) -> bool:
        return True

    def build_log_displays(self, session_username, input_text):
        return session_username, input_text

    def probe_wechat_window(self) -> bool:
        return True


def _make_client(tmp_path, svc):
    db = Database(str(tmp_path / "members.sqlite3"))
    deps.configure(db=db, wechat=svc, pipeline=None, sender=None,
                   notifier=Notifier())
    app = FastAPI()
    for router in ALL_ROUTERS:
        app.include_router(router)
    return db, TestClient(app)


def _insert_group_opp(db: Database, username: str = "21264829018@chatroom") -> dict:
    return db.insert_opportunity(
        session_username=username, session_name="配件群",
        sender_wxid="wxid_a", sender_name="张三",
        window_start="2026-11-10T14:00:00", window_end="2026-11-10T14:02:00",
        merged_count=1, raw_text="[14:02] wxid_a: 我要一箱机油",
        type_id=None, type_name="其他", summary="机油 1 箱",
        key_quote="我要一箱机油", confidence=80)


class TestMembersApi:
    def test_group_returns_member_names(self, tmp_path):
        svc = FakeSvc(members=[
            {"name": "张三", "wxid": "wxid_a", "is_owner": True},
            {"name": "李四", "wxid": "wxid_b", "is_owner": False},
        ])
        db, client = _make_client(tmp_path, svc)
        try:
            opp = _insert_group_opp(db)
            resp = client.get(f"/api/opportunities/{opp['id']}/members")
            assert resp.status_code == 200
            body = resp.json()
            assert body["is_group"] is True
            assert [m["name"] for m in body["items"]] == ["张三", "李四"]
            assert body["items"][0]["is_owner"] is True
        finally:
            deps.configure(db=None, wechat=None, pipeline=None, sender=None,
                           notifier=None)
            db.close()

    def test_direct_chat_reports_not_group(self, tmp_path):
        db, client = _make_client(tmp_path, FakeSvc())
        try:
            opp = _insert_group_opp(db, username="wxid_friend")
            body = client.get(f"/api/opportunities/{opp['id']}/members").json()
            assert body["is_group"] is False
            assert body["items"] == []
        finally:
            deps.configure(db=None, wechat=None, pipeline=None, sender=None,
                           notifier=None)
            db.close()

    def test_read_failure_returns_error_not_500(self, tmp_path):
        db, client = _make_client(tmp_path, FakeSvc(error=RuntimeError("库被占用")))
        try:
            opp = _insert_group_opp(db)
            resp = client.get(f"/api/opportunities/{opp['id']}/members")
            assert resp.status_code == 200
            body = resp.json()
            assert body["is_group"] is True
            assert "库被占用" in body["error"]
        finally:
            deps.configure(db=None, wechat=None, pipeline=None, sender=None,
                           notifier=None)
            db.close()

    def test_missing_opportunity_404(self, tmp_path):
        db, client = _make_client(tmp_path, FakeSvc())
        try:
            assert client.get("/api/opportunities/9999/members").status_code == 404
        finally:
            deps.configure(db=None, wechat=None, pipeline=None, sender=None,
                           notifier=None)
            db.close()
