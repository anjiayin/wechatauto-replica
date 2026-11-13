# -*- coding: utf-8 -*-
"""第 13 批需求回归测试（2026-11-13 用户实机反馈 7 项）。

覆盖：
1. 商机列表「来源」与「发送人」合并：后端给展示名（``session_display`` /
   ``sender_display``），企业微信联系人补成「昵称@企业简称」；前端列合并由
   tsc + 人工验证覆盖。
2. 企业微信联系人只显示昵称前半截 → ``WeChatService.display_name_with_corp``
   （整表缓存 600s，取不到企业名时与 ``get_display_name`` 一致）。
3. 给「安佳胤@我的车车」回复时消息发到了同名普通好友：
   - 搜索关键词剥离「@企业名」后缀（微信搜索框命中不了这个后缀）；
   - 企业微信会话走 ``open_chat_prefer_openim`` 消歧，绝不按名字撞第一条；
   - 打不开时明确提示「可能是同名普通好友，已取消发送」。
4. 设置页改启动端口：GET/PUT ``/api/settings/server``（保存值 + 运行值 +
   是否需重启）。
5. 商机详情「发送回复」默认人工确认：只把内容填进输入框、不按发送键，
   且不把商机置为已回复。
7. 图片漏识别：日志落 ``image_local_id``、补识别日志写清真实原因、
   ``POST /api/images/retry`` 手动补识别（原图仍未就绪时立即返回原因）。

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_batch13.py -q --basetemp=D:/tmp/kefu-pytest-b13
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.aggregator import Aggregator
from app.api import ALL_ROUTERS, deps
from app.api.router_opportunities import _display_with_corp, _is_openim
from app.config import KEY_IMAGE_VISION_ENABLED
from app.db import Database
from app.image_retry import ImageRetryQueue
from app.notifier import Notifier
from app.sender import SendTask, Sender
from app.wechat_service import WeChatService


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------
class Resp(dict):
    """WxResponse 替身：既是 dict 又有 is_success。"""

    def __init__(self, ok: bool = True, message: str = "ok", **extra) -> None:
        super().__init__(message=message, **extra)
        self.is_success = ok


class RecordingNotifier(Notifier):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, dict]] = []

    def broadcast(self, event: str, data) -> None:  # type: ignore[override]
        self.events.append((event, data))

    def of(self, event: str) -> list[dict]:
        return [d for e, d in self.events if e == event]


class FakeGui:
    """WeChatGUI 替身：记录调用，支持企业微信消歧与「只填入不发送」。"""

    def __init__(self, *, open_ok: bool = True, openim_ok: bool = True,
                 send_ok: bool = True, fill_ok: bool = True,
                 openim_capable: bool = True,
                 input_only_capable: bool = True) -> None:
        self.open_ok = open_ok
        self.openim_ok = openim_ok
        self.send_ok = send_ok
        self.fill_ok = fill_ok
        self.calls: list[tuple] = []
        # guia.last_open_error() 的替身：tree_not_ready / identity_mismatch /
        # corp_mismatch / ""（见 wechatauto.guia.open_chat_prefer_openim）
        self.open_error = ""
        if not openim_capable:
            # 模拟旧版 wechatauto：没有消歧方法，调用方应退回 open_chat
            self.open_chat_prefer_openim = None  # type: ignore[assignment]
        if not input_only_capable:
            self.input_only = None  # type: ignore[assignment]

    # 基础三件套
    def is_alive(self) -> bool:
        return True

    def bring_to_front(self) -> None:
        self.calls.append(("front",))

    def ensure_visible(self) -> bool:
        return True

    def open_chat(self, name: str) -> bool:
        self.calls.append(("open", name))
        return self.open_ok

    def open_chat_prefer_openim(self, name: str, corp: str = "") -> bool:
        self.calls.append(("open_openim", name, corp))
        return self.openim_ok

    def last_open_error(self) -> str:
        return self.open_error

    def send_msg(self, text: str, who: str | None = None, verify: bool = False,
                 **kwargs):
        self.calls.append(("send", text, who, kwargs.get("who_username")))
        return Resp(self.send_ok, "消息已发送并确认" if self.send_ok else "失败")

    def input_only(self, text: str, who: str | None = None,
                   who_username: str | None = None):
        self.calls.append(("input_only", text, who, who_username))
        return Resp(self.fill_ok, f"已填入微信输入框（{who}），请在微信里点发送",
                    manual=True)


def make_sender(gui: FakeGui) -> tuple[Sender, Database]:
    db = Database(":memory:")
    sender = Sender(db, RecordingNotifier())
    sender._gui = gui
    return sender, db


def seed_opp(db: Database, **kw) -> dict:
    params = dict(
        session_username="wxid_ajy@openim", session_name="安佳胤@我的车车",
        sender_wxid="wxid_ajy@openim", sender_name="安佳胤",
        window_start="2026-11-13T12:00:00", window_end="2026-11-13T12:01:00",
        merged_count=1, raw_text="[12:00] 有没有帕萨特前杠",
        type_id=None, type_name="汽车配件", summary="求购前杠",
        key_quote="有没有帕萨特前杠", confidence=90)
    params.update(kw)
    return db.insert_opportunity(**params)


# ---------------------------------------------------------------------------
# 需求 2：企业微信显示名「昵称@企业简称」
# ---------------------------------------------------------------------------
class FakeWxForCorp:
    """WeChatDB 替身：昵称 + 企业简称表（记录调用次数以验证缓存）。"""

    def __init__(self) -> None:
        self.corp_calls = 0

    def get_nickname(self, username: str) -> str:
        return {"wxid_ajy@openim": "安佳胤", "wxid_ajy": "安佳胤"}.get(
            username, "")

    def get_openim_corp_names(self) -> dict:
        self.corp_calls += 1
        return {"wxid_ajy@openim": "我的车车"}


class TestDisplayNameWithCorp:
    def test_openim_name_gets_corp(self):
        db = Database(":memory:")
        svc = WeChatService(db, Aggregator(lambda u: None))
        svc._wxdb = FakeWxForCorp()
        assert svc.display_name_with_corp("wxid_ajy@openim") == "安佳胤@我的车车"

    def test_normal_friend_untouched(self):
        db = Database(":memory:")
        svc = WeChatService(db, Aggregator(lambda u: None))
        svc._wxdb = FakeWxForCorp()
        assert svc.display_name_with_corp("wxid_ajy") == "安佳胤"

    def test_corp_map_cached(self):
        """商机列表一页几十行：企业简称表只能读一次。"""
        db = Database(":memory:")
        svc = WeChatService(db, Aggregator(lambda u: None))
        wx = FakeWxForCorp()
        svc._wxdb = wx
        for _ in range(5):
            svc.display_name_with_corp("wxid_ajy@openim")
        assert wx.corp_calls == 1

    def test_corp_failure_falls_back_to_nick(self):
        class Broken(FakeWxForCorp):
            def get_openim_corp_names(self):
                raise RuntimeError("contact.db 被占用")

        db = Database(":memory:")
        svc = WeChatService(db, Aggregator(lambda u: None))
        svc._wxdb = Broken()
        assert svc.display_name_with_corp("wxid_ajy@openim") == "安佳胤"


class TestDisplayHelperInRouter:
    def test_is_openim(self):
        assert _is_openim("wxid_x@openim")
        assert _is_openim("kf@kefu.openim")
        assert not _is_openim("wxid_x")
        assert not _is_openim("123@chatroom")

    def test_helper_appends_corp_for_stored_bare_name(self):
        svc = SimpleNamespace(display_name_with_corp=lambda u: "安佳胤@我的车车")
        assert _display_with_corp(svc, "wxid_a@openim", "安佳胤") == "安佳胤@我的车车"

    def test_helper_keeps_already_full_name(self):
        svc = SimpleNamespace(display_name_with_corp=lambda u: "不该被调用")
        got = _display_with_corp(svc, "wxid_a@openim", "安佳胤@我的车车")
        assert got == "安佳胤@我的车车"

    def test_helper_tolerates_missing_service(self):
        # 列表接口不依赖微信服务：没注入时原样返回已存名字
        assert _display_with_corp(None, "wxid_a@openim", "安佳胤") == "安佳胤"
        assert _display_with_corp(SimpleNamespace(), "wxid_a", "张三") == "张三"


# ---------------------------------------------------------------------------
# 需求 1：列表接口带展示名（存量商机也能补全企业名）
# ---------------------------------------------------------------------------
class TestListDisplays:
    def _env(self, tmp_path, svc=None):
        db = Database(str(tmp_path / "t.sqlite3"))
        notifier = Notifier()
        deps.configure(db=db, wechat=svc, pipeline=None, sender=None,
                       notifier=notifier)
        app = FastAPI()
        for router in ALL_ROUTERS:
            app.include_router(router)
        return SimpleNamespace(db=db, client=TestClient(app))

    def test_list_and_detail_carry_display_names(self, tmp_path):
        svc = SimpleNamespace(
            display_name_with_corp=lambda u: "安佳胤@我的车车",
            session_exists=lambda u: True,
            build_log_displays=lambda u, t: ("安佳胤@我的车车", t))
        env = self._env(tmp_path, svc)
        try:
            opp = seed_opp(env.db, session_name="安佳胤", sender_name="安佳胤")
            item = env.client.get("/api/opportunities").json()["items"][0]
            assert item["session_display"] == "安佳胤@我的车车"
            assert item["sender_display"] == "安佳胤@我的车车"
            detail = env.client.get(f"/api/opportunities/{opp['id']}").json()
            assert detail["session_display"] == "安佳胤@我的车车"
        finally:
            deps.configure(db=None, wechat=None, pipeline=None, sender=None,
                           notifier=None)
            env.db.close()

    def test_list_works_without_wechat_service(self, tmp_path):
        """微信服务没起来时列表照常返回（展示名退化为已存名字）。"""
        env = self._env(tmp_path, None)
        try:
            seed_opp(env.db, session_username="123@chatroom",
                     session_name="拆车件群", sender_wxid="wxid_a",
                     sender_name="张三")
            resp = env.client.get("/api/opportunities")
            assert resp.status_code == 200
            item = resp.json()["items"][0]
            assert item["session_display"] == "拆车件群"
            assert item["sender_display"] == "张三"
        finally:
            deps.configure(db=None, wechat=None, pipeline=None, sender=None,
                           notifier=None)
            env.db.close()


# ---------------------------------------------------------------------------
# 需求 3：企业微信发送消歧
# ---------------------------------------------------------------------------
class TestOpenimSend:
    def test_chat_name_strips_corp_suffix(self):
        task = SendTask(opportunity_id=1, session_username="wxid_ajy@openim",
                        session_name="安佳胤@我的车车", text="在")
        assert Sender._chat_name(task) == "安佳胤"

    def test_chat_name_keeps_group_name(self):
        task = SendTask(opportunity_id=1, session_username="1@chatroom",
                        session_name="拆车件群", text="在")
        assert Sender._chat_name(task) == "拆车件群"

    def test_uses_prefer_openim_for_openim_session(self):
        gui = FakeGui()
        sender, db = make_sender(gui)
        opp = seed_opp(db)
        sender._process(SendTask(opportunity_id=opp["id"],
                                 session_username="wxid_ajy@openim",
                                 session_name="安佳胤@我的车车", text="有货"))
        names = [c[0] for c in gui.calls]
        # 关键词是昵称（不带 @企业名），企业名作为消歧提示传下去
        assert ("open_openim", "安佳胤", "我的车车") in gui.calls
        assert ("open", "安佳胤") not in gui.calls
        assert "send" in names

    def test_falls_back_to_plain_open_when_gui_old(self):
        """旧版 wechatauto 没有消歧方法：退回 open_chat，不报错。"""
        gui = FakeGui(openim_capable=False)
        sender, db = make_sender(gui)
        opp = seed_opp(db)
        sender._process(SendTask(opportunity_id=opp["id"],
                                 session_username="wxid_ajy@openim",
                                 session_name="安佳胤@我的车车", text="有货"))
        assert ("open", "安佳胤") in gui.calls

    def test_same_name_friend_is_refused(self):
        """同名普通好友：打开失败 → 明确提示，且绝不发送。"""
        gui = FakeGui(openim_ok=False)
        sender, db = make_sender(gui)
        opp = seed_opp(db)
        sender._process(SendTask(opportunity_id=opp["id"],
                                 session_username="wxid_ajy@openim",
                                 session_name="安佳胤@我的车车", text="有货"))
        assert not any(c[0] == "send" for c in gui.calls)
        events = sender._notifier.of("send_result")
        assert events and events[0]["ok"] is False
        assert "同名" in events[0]["message"]


# ---------------------------------------------------------------------------
# 需求 5：默认人工确认（只填输入框，不发送）
# ---------------------------------------------------------------------------
class TestManualConfirm:
    def test_fills_without_sending(self):
        gui = FakeGui()
        sender, db = make_sender(gui)
        opp = seed_opp(db, session_username="123@chatroom",
                       session_name="拆车件群")
        sender._process(SendTask(opportunity_id=opp["id"],
                                 session_username="123@chatroom",
                                 session_name="拆车件群", text="明天发货",
                                 manual_confirm=True))
        assert ("input_only", "明天发货", "拆车件群", "123@chatroom") in gui.calls
        assert not any(c[0] == "send" for c in gui.calls)
        events = sender._notifier.of("send_result")
        assert events and events[0]["ok"] is True and events[0]["manual"] is True
        # 没真正发出：状态必须还是待处理，不能骗用户「已回复」
        assert db.get_opportunity(opp["id"])["status"] == "pending"

    def test_manual_failure_reported(self):
        gui = FakeGui(fill_ok=False)
        sender, db = make_sender(gui)
        opp = seed_opp(db, session_username="123@chatroom",
                       session_name="拆车件群")
        sender._process(SendTask(opportunity_id=opp["id"],
                                 session_username="123@chatroom",
                                 session_name="拆车件群", text="x",
                                 manual_confirm=True))
        events = sender._notifier.of("send_result")
        assert events and events[0]["ok"] is False
        assert "填入输入框失败" in events[0]["message"]

    def test_gui_without_input_only_says_so(self):
        gui = FakeGui(input_only_capable=False)
        sender, db = make_sender(gui)
        opp = seed_opp(db, session_username="123@chatroom",
                       session_name="拆车件群")
        sender._process(SendTask(opportunity_id=opp["id"],
                                 session_username="123@chatroom",
                                 session_name="拆车件群", text="x",
                                 manual_confirm=True))
        events = sender._notifier.of("send_result")
        assert events and "不支持" in events[0]["message"]

    def test_auto_send_still_marks_replied(self):
        gui = FakeGui()
        sender, db = make_sender(gui)
        opp = seed_opp(db, session_username="123@chatroom",
                       session_name="拆车件群")
        sender._process(SendTask(opportunity_id=opp["id"],
                                 session_username="123@chatroom",
                                 session_name="拆车件群", text="明天发货",
                                 manual_confirm=False))
        assert db.get_opportunity(opp["id"])["status"] == "replied"


# ---------------------------------------------------------------------------
# 需求 4：启动端口设置接口
# ---------------------------------------------------------------------------
class TestServerPortSettings:
    @pytest.fixture()
    def env(self, tmp_path):
        db = Database(str(tmp_path / "t.sqlite3"))
        deps.configure(db=db, wechat=None, pipeline=None, sender=None,
                       notifier=Notifier())
        app = FastAPI()
        for router in ALL_ROUTERS:
            app.include_router(router)
        yield SimpleNamespace(db=db, client=TestClient(app))
        deps.configure(db=None, wechat=None, pipeline=None, sender=None,
                       notifier=None)
        db.close()

    def test_get_returns_saved_and_running(self, env):
        body = env.client.get("/api/settings/server").json()
        assert body["port"] == 8765          # 默认值
        assert body["default_port"] == 8765
        assert body["address"] == "http://127.0.0.1:8765"
        # TestClient 的 scope server 是 testserver:80 → 与保存值不同
        assert body["restart_required"] is True

    def test_put_persists_port(self, env):
        resp = env.client.put("/api/settings/server", json={"port": 9000})
        assert resp.status_code == 200
        assert resp.json()["port"] == 9000
        assert resp.json()["address"] == "http://127.0.0.1:9000"
        # 写进 settings 表的 run.port（run.py 启动时按这个键读）
        assert env.db.get_setting("run.port") == "9000"

    def test_put_rejects_out_of_range(self, env):
        assert env.client.put("/api/settings/server",
                              json={"port": 0}).status_code == 422
        assert env.client.put("/api/settings/server",
                              json={"port": 70000}).status_code == 422

    def test_reset_to_default(self, env):
        env.client.put("/api/settings/server", json={"port": 9000})
        body = env.client.put("/api/settings/server",
                              json={"port": 8765}).json()
        assert body["port"] == 8765


# ---------------------------------------------------------------------------
# 需求 7：图片漏识别（local_id 落库 + 手动补识别）
# ---------------------------------------------------------------------------
def make_image_svc() -> WeChatService:
    db = Database(":memory:")
    svc = WeChatService(db, Aggregator(lambda u: None))
    return svc


class TestImageLogLocalId:
    def test_skipped_log_carries_image_local_id(self):
        svc = make_image_svc()
        svc._log_image_skipped("111@chatroom", "wxid_a", "图片", "[图片]",
                               "图片读取失败或不完整", 21652)
        _total, items = svc._db.list_analysis_logs(status="skipped")
        assert items[0]["image_local_id"] == 21652
        # 语音列不受影响（两种媒体各自的定位键）
        assert items[0]["voice_local_id"] is None

    def test_retry_image_refuses_when_not_ready(self):
        svc = make_image_svc()
        # 打桩到 with_reason 版（生产代码用它取图，才能拿到失败原因）
        svc.get_image_bytes_with_reason = (  # type: ignore[assignment]
            lambda u, l: (None, "原图与缩略图均不完整"))
        ok, message = svc.retry_image("111@chatroom", 21652)
        assert ok is False
        assert "原图与缩略图均不完整" in message
        # 提示必须给出可操作下一步
        assert "点开" in message

    def test_retry_image_rejects_bad_args(self):
        svc = make_image_svc()
        assert svc.retry_image("", 1)[0] is False
        assert svc.retry_image("111@chatroom", 0)[0] is False

    def test_retry_image_respects_switch(self):
        svc = make_image_svc()
        svc._db.set_setting(KEY_IMAGE_VISION_ENABLED, "0")
        ok, message = svc.retry_image("111@chatroom", 21652)
        assert ok is False and "开关" in message


class TestImageRetryApi:
    @pytest.fixture()
    def env(self, tmp_path):
        db = Database(str(tmp_path / "t.sqlite3"))
        svc = SimpleNamespace(
            retry_image=lambda u, l: (False, "这张图仍未就绪：原图未下载"))
        deps.configure(db=db, wechat=svc, pipeline=None, sender=None,
                       notifier=Notifier())
        app = FastAPI()
        for router in ALL_ROUTERS:
            app.include_router(router)
        yield SimpleNamespace(db=db, client=TestClient(app))
        deps.configure(db=None, wechat=None, pipeline=None, sender=None,
                       notifier=None)
        db.close()

    def test_returns_reason_not_500(self, env):
        resp = env.client.post("/api/images/retry",
                               json={"username": "111@chatroom",
                                     "local_id": 21652})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "原图未下载" in body["error"]


class TestImageRetryGiveUp:
    """需求 7：退避用尽必须落一条「已放弃 + 怎么手动补」的可见终态日志。

    否则用户看到最后一条永远是「第 4 次重试：图片仍未就绪（…），继续等待后
    自动补识别」，会一直等一个永远不会来的结果——微信原图是按需下载的，
    不去点开就永远不会就绪。
    """

    TASK = {"username": "111@chatroom", "local_id": 21652,
            "sender_wxid": "wxid_a", "mtype": "图片", "content": "[图片]"}

    def test_given_up_log_is_visible_and_actionable(self):
        svc = make_image_svc()
        svc._on_image_retry_given_up("111@chatroom", 21652, self.TASK, 4)
        _total, items = svc._db.list_analysis_logs(status="skipped")
        text = items[0]["error"] or ""
        assert "4 次" in text
        assert "重新识别" in text          # 给出唯一有效的补救动作
        assert "按需下载" in text          # 解释「为什么不会自己好」
        assert items[0]["image_local_id"] == 21652

    def test_given_up_log_survives_bad_payload(self):
        """payload 缺字段也不能抛（回调在 worker 线程里，抛了就没人兜）。"""
        svc = make_image_svc()
        svc._on_image_retry_given_up("111@chatroom", 21652, {}, 3)
        _total, items = svc._db.list_analysis_logs(status="skipped")
        assert items and "3 次" in (items[0]["error"] or "")

    def test_queue_calls_back_when_retries_exhausted(self):
        seen = []
        done = threading.Event()

        def handler(task):
            return "retry"

        def on_give_up(username, local_id, task, attempts):
            seen.append((username, local_id, attempts))
            done.set()

        queue = ImageRetryQueue(handler, delays=(0.1, 0.1),
                                on_give_up=on_give_up)
        queue.start()
        try:
            assert queue.enqueue("111@chatroom", 21652, self.TASK) is True
            assert done.wait(timeout=5), "重试用尽后应回调 on_give_up"
        finally:
            queue.stop()
        assert seen == [("111@chatroom", 21652, 2)]
        assert queue.status()["abandoned"] == 1

    def test_queue_abort_does_not_call_given_up(self):
        """abort（额度/Key 之类）自己已落日志，不能重复记成「重试用尽」。"""
        seen = []
        queue = ImageRetryQueue(lambda t: "abort", delays=(0.1,),
                                on_give_up=lambda *a: seen.append(a))
        queue.start()
        try:
            assert queue.enqueue("111@chatroom", 21652, self.TASK) is True
            for _ in range(50):
                if queue.status()["abandoned"] >= 1:
                    break
                threading.Event().wait(0.05)
        finally:
            queue.stop()
        assert seen == []


class TestOpenFailureMessages:
    """打开会话失败的用户可见文案（批次 13 追加，实机 19:01 反馈）。

    现场：微信已经打开到「安佳胤@我的车车」，但提示「无法定位会话」，
    而且从微信到前台到开始打字之间空转 25 秒。根因是控件树未物化 +
    老版本标题区 AID 失效，两类失败必须给出不同、可操作的提示。
    """

    def _run(self, gui):
        sender, db = make_sender(gui)
        opp = seed_opp(db, session_username="wxid_ajy@openim",
                       session_name="安佳胤@我的车车")
        sender._process(SendTask(opportunity_id=opp["id"],
                                 session_username="wxid_ajy@openim",
                                 session_name="安佳胤@我的车车", text="有货"))
        return sender._notifier.of("send_result")[0]["message"]

    def test_tree_not_ready_says_waiting_not_missing(self):
        """控件树没就绪 ≠ 会话不存在：不能报「无法定位会话」让人以为发错人。"""
        gui = FakeGui(openim_ok=False)
        gui.open_error = "tree_not_ready"
        msg = self._run(gui)
        assert "控件树" in msg and "30 秒" in msg
        assert "无法定位会话" not in msg

    def test_identity_mismatch_explains_plain_friend(self):
        gui = FakeGui(openim_ok=False)
        gui.open_error = "identity_mismatch"
        msg = self._run(gui)
        assert "普通好友" in msg and "手动打开" in msg

    def test_corp_mismatch_is_reported(self):
        gui = FakeGui(openim_ok=False)
        gui.open_error = "corp_mismatch"
        msg = self._run(gui)
        assert "不属于该企业微信" in msg

    def test_generic_openim_failure_keeps_warning(self):
        gui = FakeGui(openim_ok=False)          # open_error 为空 = 没搜到
        msg = self._run(gui)
        assert "无法定位会话" in msg and "同名" in msg
