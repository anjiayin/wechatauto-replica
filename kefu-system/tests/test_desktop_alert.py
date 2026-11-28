# -*- coding: utf-8 -*-
"""桌面提醒（app.desktop_alert）单元测试：标题/正文规整与原子落盘。"""
from __future__ import annotations

import asyncio
import json

from app import desktop_alert
from app.config import KEY_NOTIFY_SYSTEM
from app.db import Database
from app.notifier import Notifier
from app.pipeline import Pipeline


def test_alert_title_matches_frontend_semantics():
    assert desktop_alert.alert_title("客户投诉") == "收到新的客户投诉"
    assert desktop_alert.alert_title(None) == "收到新商机"
    assert desktop_alert.alert_title("") == "收到新商机"
    assert desktop_alert.alert_title("未知类型") == "收到新商机"


def test_build_alert_uses_first_summary_line():
    alert = desktop_alert.build_alert({
        "id": 7,
        "type_name": "原料求购",
        "session_name": "采购群",
        "summary": "\n  求购铝锭一批\n第二批 5 吨",
    })
    assert alert["id"] == 7
    assert alert["title"] == "收到新的原料求购"
    assert alert["body"] == "采购群：求购铝锭一批"
    assert isinstance(alert["at"], int)


def test_build_alert_without_session_or_summary():
    alert = desktop_alert.build_alert({"id": 8, "type_name": None})
    assert alert["body"] == ""


def test_request_desktop_alert_writes_json_atomically(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_alert, "DATA_DIR", tmp_path)
    assert desktop_alert.request_desktop_alert({
        "id": 9,
        "type_name": "机械加工订单",
        "session_name": "加工群",
        "summary": "需要 CNC 加工 500 件",
    }) is True
    path = tmp_path / desktop_alert.ALERT_FILE_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["id"] == 9
    assert data["title"] == "收到新的机械加工订单"
    assert data["body"] == "加工群：需要 CNC 加工 500 件"
    # 临时文件已被原子替换，不留残渣
    assert not list(tmp_path.glob(".native_alert-*.tmp"))


def test_request_desktop_alert_failure_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_alert, "DATA_DIR", tmp_path)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(desktop_alert.tempfile, "mkstemp", boom)
    assert desktop_alert.request_desktop_alert({"id": 1}) is False


class _FakeWS:
    async def accept(self) -> None:
        pass


def test_notifier_client_count_tracks_connections():
    notifier = Notifier()
    assert notifier.client_count == 0

    async def scenario() -> None:
        ws = _FakeWS()
        await notifier.connect(ws)              # type: ignore[arg-type]
        assert notifier.client_count == 1
        notifier.disconnect(ws)                 # type: ignore[arg-type]
        assert notifier.client_count == 0

    asyncio.run(scenario())


def test_pipeline_only_alerts_when_page_closed_and_enabled(
        tmp_path, monkeypatch):
    db = Database(str(tmp_path / "alert_pipeline.sqlite3"))
    notifier = Notifier()
    pipeline = Pipeline(db, notifier)
    calls: list[dict] = []
    monkeypatch.setattr(
        desktop_alert, "request_desktop_alert",
        lambda opp: calls.append(opp) or True)
    opp = {"id": 3, "type_name": "客户投诉",
           "session_name": "售后群", "summary": "客户要求退款"}

    # 页面未打开 + 开关默认开启 → 请求桌面提醒
    pipeline._maybe_desktop_alert(opp)
    assert calls == [opp]

    # 开关关闭 → 不提醒
    db.set_setting(KEY_NOTIFY_SYSTEM, "0")
    pipeline._maybe_desktop_alert(opp)
    assert len(calls) == 1

    # 页面已连接（WS）→ 不重复弹系统通知
    db.set_setting(KEY_NOTIFY_SYSTEM, "1")
    ws = _FakeWS()

    async def connect() -> None:
        await notifier.connect(ws)           # type: ignore[arg-type]

    asyncio.run(connect())
    pipeline._maybe_desktop_alert(opp)
    assert len(calls) == 1

    # 页面关闭（断开）→ 恢复系统提醒
    notifier.disconnect(ws)                  # type: ignore[arg-type]
    pipeline._maybe_desktop_alert(opp)
    assert len(calls) == 2
    db.close()