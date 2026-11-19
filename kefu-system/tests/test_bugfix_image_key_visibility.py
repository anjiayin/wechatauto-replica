# -*- coding: utf-8 -*-
"""Bugfix v1.2.6：删除 image_keys.json 后图片不识别且界面无提醒。

真因（见交付报告）：用户删除 ``image_keys.json`` 后，这台机器上 cfgDword 派生
不可用（extract_master_key() 返回 None）、内存密钥未捕获，图片 AES 密钥全部
取不到 → 图片消息虽然被 Listener 捕获并入识别队列，但取图失败，原因文案却是
误导性的「原图与缩略图均不完整」；而唯一的密钥提醒只藏在设置页某个 tab 里，
用户在商机列表/分析日志页完全看不到。

本测试覆盖两处最小修复：
1. ``/api/status`` 新增 ``image_key_ready`` 字段，供前端做**全局**横幅提醒；
   异常/替身缺方法时按 True 降级，避免误挂永久横幅。
2. ``get_image_bytes_with_reason`` 在取图失败且密钥未就绪时，把原因换成可操作
   的「请在微信中打开一张图片」提示；密钥就绪/探测异常时保留原文案。

不连真实微信库、不发消息；依赖全部用替身注入。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.router_system import _image_key_ready, router as system_router
from app.db import Database
from app.notifier import Notifier
from app.schemas import StatusOut
from app.wechat_service import WeChatService


class _Svc:
    listener_running = True
    monitored_count = 1
    available = True
    watermark_saved_at = ""
    degraded_reason = ""

    def __init__(self, key_status=None, raises=False):
        self._key_status = key_status if key_status is not None else {"ready": True, "source": ""}
        self._raises = raises

    def probe_wechat_window(self):
        return False

    def cache_status(self):
        return []

    def image_key_status(self):
        if self._raises:
            raise RuntimeError("媒体库不可用")
        return self._key_status


class _Pipeline:
    queue_size = 0
    queue_max = 100
    dropped_total = 0
    busy = False
    analysis_health = {}


@pytest.fixture()
def cleanup():
    yield
    deps.configure(db=None, wechat=None, pipeline=None, sender=None, notifier=None)


def _client(tmp_path, svc):
    db = Database(str(tmp_path / "bugfix.sqlite3"))
    deps.configure(db=db, wechat=svc, pipeline=_Pipeline(),
                   sender=None, notifier=Notifier())
    app = FastAPI()
    app.include_router(system_router)
    return TestClient(app), db


def test_status_exposes_image_key_ready_false(tmp_path, cleanup):
    """密钥未就绪时 /api/status.image_key_ready=False → 前端全局提醒。"""
    client, db = _client(tmp_path, _Svc(key_status={"ready": False, "source": ""}))
    try:
        body = client.get("/api/status").json()
        assert body["image_key_ready"] is False
    finally:
        db.close()


def test_status_image_key_ready_true(tmp_path, cleanup):
    client, db = _client(tmp_path, _Svc(key_status={"ready": True, "source": "cache"}))
    try:
        assert client.get("/api/status").json()["image_key_ready"] is True
    finally:
        db.close()


def test_status_defaults_ready_when_method_missing_or_error(tmp_path, cleanup):
    """替身/异常一律按就绪处理，避免在不支持该能力的部署上误挂永久横幅。"""
    class _NoMethod(_Svc):
        image_key_status = None

    for svc in (_NoMethod(), _Svc(raises=True)):
        assert _image_key_ready(svc) is True
    assert StatusOut().image_key_ready is True


def test_image_reason_surfaces_missing_key(monkeypatch):
    """取图失败 + 密钥未就绪 → 原因点明「请在微信中打开任意一张图片」。"""
    svc = WeChatService(Database(":memory:"), lambda _u: None)

    class _DL:
        def load_image(self, user, local_id):
            return None, None, "原图与缩略图均不完整，返回占位"

    monkeypatch.setattr(svc, "_get_media_downloader", lambda: _DL())
    monkeypatch.setattr(svc, "image_key_status",
                        lambda: {"ready": False, "source": ""})
    svc._wxdb = object()  # 去掉「微信库未就绪」的提前返回

    data, reason = svc.get_image_bytes_with_reason("u@chatroom", 123)
    assert data is None
    assert "密钥" in reason and "微信中打开" in reason
    assert "原图与缩略图均不完整" not in reason


def test_image_reason_keeps_original_when_key_ready(monkeypatch):
    """密钥就绪时取图失败保留底层原因（如原图确实未下载完）。"""
    svc = WeChatService(Database(":memory:"), lambda _u: None)

    class _DL:
        def load_image(self, user, local_id):
            return None, None, "原图与缩略图均不完整，返回占位"

    monkeypatch.setattr(svc, "_get_media_downloader", lambda: _DL())
    monkeypatch.setattr(svc, "image_key_status",
                        lambda: {"ready": True, "source": "memory"})
    svc._wxdb = object()

    data, reason = svc.get_image_bytes_with_reason("u@chatroom", 123)
    assert data is None
    assert reason == "原图与缩略图均不完整，返回占位"


def test_image_reason_keeps_original_when_probe_fails(monkeypatch):
    """密钥状态探测异常时保留原文案，绝不影响主流程。"""
    svc = WeChatService(Database(":memory:"), lambda _u: None)

    class _DL:
        def load_image(self, user, local_id):
            return None, None, "原图与缩略图均不完整，返回占位"

    def _boom():
        raise RuntimeError("probe failed")

    monkeypatch.setattr(svc, "_get_media_downloader", lambda: _DL())
    monkeypatch.setattr(svc, "image_key_status", _boom)
    svc._wxdb = object()

    data, reason = svc.get_image_bytes_with_reason("u@chatroom", 123)
    assert data is None
    assert reason == "原图与缩略图均不完整，返回占位"
