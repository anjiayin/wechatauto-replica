# -*- coding: utf-8 -*-
"""Bugfix v1.2.7：图片密钥缺失导致的「静默丢失」与「重新检查无效」。

现场实测（2026-11-19）：生产 ``image_keys.json`` 缺失 + cfgDword 派生不可用
时，每张 V2(AES) 图片的取图会在解密路径里做 ``_scan_aes_key(monitor=True)``
全量内存扫描（5 个 Weixin 进程 ~1.3GB，一轮 ~110s）+ 账号 XOR 学习的「实测」
对每个候选再触发整套扫描，单次取图拖 30+ 分钟——表现为「图片永不处理、
日志静默」；同时设置页「重新检查」只读状态、点击毫无反应。

三处修复（本文件逐条验证）：
1. ``wechatauto.media._resolve_aes_key`` 不再做内存扫描（解密路径只查
   注入/派生/持久化三种零成本来源），密钥缺失时**秒级**抛出可操作的
   RuntimeError → 上层快速失败并排入补识别队列（fail-visible）；
2. ``_scan_aes_key`` 的 ``monitor_timeout`` 成为真实墙钟上限（逐内存区域
   检查 deadline），扫描提示改走日志（pythonw 下 print 用户看不到）；
3. ``WeChatService.recheck_image_key``：后台线程防重入地做一次带预算的
   主动捕获（``detect_image_key(refresh=True, monitor_timeout=...)``），
   成功/失败都落 app 日志并写进 ``image_key_status()`` 的 recheck_* 字段；
   ``POST /api/system/image-key-recheck`` 暴露给设置页「重新检查」按钮。

不连真实微信库、不发消息、不做真实内存扫描；依赖全部用替身注入。
"""
from __future__ import annotations

import struct
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.router_system import router as system_router
from app.db import Database
from app.notifier import Notifier
from app.schemas import ImageKeyStatusOut
from app.wechat_service import WeChatService
from wechatauto.media import V2_MAGIC, MediaDownloader


# ---------------------------------------------------------------------------
# 1) 解密路径快速失败：密钥缺失时秒级抛错，绝不触发内存扫描
# ---------------------------------------------------------------------------

class _FakeDB:
    """MediaDownloader 所需的最小 DB 替身（无 WeChatDB 依赖）。"""

    def __init__(self, tmp_path):
        self.workdir = str(tmp_path)
        self.account_dir = str(tmp_path / "acct")
        self.account = "wxid_fake_ac0e"
        self.wxid = "wxid_fake"
        self.cfg_dword = None          # 现场：cfgDword 派生不可用


def _write_v2_dat(path, aes_size=16, xor_size=0):
    """写一个最小 V2 头的 .dat（15 字节头：6 magic + 8 尺寸 + 1 保留）。"""
    payload = b"\x11" * 64
    with open(path, "wb") as f:
        f.write(V2_MAGIC)
        f.write(struct.pack("<LL", aes_size, xor_size))
        f.write(b"\x00")              # V2 密文从第 15 字节开始
        f.write(payload)
    return str(path)


def test_decrypt_v2_fast_fail_without_scan(tmp_path):
    """AES 密钥缺失 → decrypt_image 立刻抛「重新检查」指引，且不做内存扫描。"""
    dl = MediaDownloader(_FakeDB(tmp_path), save_dir=str(tmp_path))
    # 若底层仍会尝试内存扫描，_scan_aes_key 会打开真实 Weixin 进程（本机在
    # 跑微信）并耗时分钟级；用哨兵把它变成「被调用即失败」，保证测试既快
    # 又能证明解密路径根本没碰扫描。
    def _boom(monitor=False, monitor_timeout=120.0):
        raise AssertionError("解密路径不允许触发内存扫描")

    dl._scan_aes_key = _boom
    dat = _write_v2_dat(tmp_path / ("deadbeef" * 4 + ".dat"))

    start = time.monotonic()
    with pytest.raises(RuntimeError) as excinfo:
        dl.decrypt_image(dat, None, xor_key=0x88)
    elapsed = time.monotonic() - start

    assert elapsed < 5.0, f"解密路径快速失败耗时异常：{elapsed:.1f}s"
    text = str(excinfo.value)
    assert "AES" in text and "重新检查" in text


def test_decrypt_v2_uses_persisted_key_without_scan(tmp_path):
    """持久化密钥存在 → 解密路径直接命中缓存来源，不触发扫描。"""
    import json

    workdir = tmp_path / "cache"
    workdir.mkdir()
    db = _FakeDB(tmp_path)
    db.workdir = str(workdir)
    dl = MediaDownloader(db, save_dir=str(tmp_path))

    key = "0123456789abcdef"
    # 探针与密文必须配套才能通过 _validate_key 反测：用真密钥加密探针块。
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    probe_plain = b"\xff\xd8\xff\xe0" + b"\x00" * 12          # JPEG SOI 头
    enc = Cipher(algorithms.AES(key.encode()), modes.ECB()).encryptor()
    probe_ct = enc.update(probe_plain) + enc.finalize()
    dat_body = V2_MAGIC + struct.pack("<LL", 16, 0) + b"\x00" \
        + probe_ct + b"\x99" * 32
    dat = tmp_path / ("cafebabe" * 4 + ".dat")
    dat.write_bytes(dat_body)

    with open(workdir / "image_keys.json", "w", encoding="utf-8") as f:
        json.dump({db.account: key}, f)
    # 探针缓存指向我们构造的密文块（避免 _collect_templates 扫盘）
    dl._key_probe = probe_ct

    def _boom(monitor=False, monitor_timeout=120.0):
        raise AssertionError("解密路径不允许触发内存扫描")

    dl._scan_aes_key = _boom
    out = dl.decrypt_image(str(dat), None, xor_key=0x88)
    assert out[:4] == b"\xff\xd8\xff\xe0"


# ---------------------------------------------------------------------------
# 2) 扫描墙钟预算：monitor_timeout 现在约束整个调用（含等待循环）
# ---------------------------------------------------------------------------

class _FakePidsDB:
    cfg_dword = None
    wxid = "wxid_fake"

    @staticmethod
    def _find_weixin_pids():
        return [999999]        # 不存在的 PID：OpenProcess 失败，扫描立即返回


def test_scan_aes_key_monitor_wait_is_bounded(tmp_path):
    """monitor=True 且始终扫不到密钥 → 调用在 monitor_timeout 墙钟内返回。"""
    dl = MediaDownloader(_FakePidsDB(), save_dir=str(tmp_path))
    # 探针给一个非 JPEG 结果，保证任何候选都不会命中
    dl._probe_cache = (time.monotonic() + 3600, [b"\x00" * 16])

    start = time.monotonic()
    out = dl._scan_aes_key(monitor=True, monitor_timeout=2.0)
    elapsed = time.monotonic() - start

    assert out is None
    assert elapsed < 10.0, f"monitor 等待循环未被墙钟约束：{elapsed:.1f}s"


# ---------------------------------------------------------------------------
# 3) 「重新检查」主动捕获：后台线程、防重入、结果可见
# ---------------------------------------------------------------------------

class _RecheckDL:
    """detect_image_key 替身：可编程返回值与耗时（补齐密钥探测字段）。"""

    _img_key = None
    _image_key = None

    def __init__(self, result=None, delay=0.0):
        self.result = result
        self.delay = delay
        self.calls = []

    def _derive_cfg_key(self):
        return None

    def _load_persisted_key(self):
        return None

    def detect_image_key(self, refresh=False, monitor_timeout=120.0):
        self.calls.append((refresh, monitor_timeout))
        if self.delay:
            time.sleep(self.delay)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _make_svc(monkeypatch, dl):
    svc = WeChatService(Database(":memory:"), lambda _u: None)
    monkeypatch.setattr(svc, "_get_media_downloader", lambda: dl)
    return svc


def test_recheck_success_records_state(monkeypatch):
    """捕获成功：last_ok=True、running 复位、detect_image_key 带预算调用。"""
    dl = _RecheckDL(result=("0123456789abcdef", 0xf9))
    svc = _make_svc(monkeypatch, dl)

    out = svc.recheck_image_key(timeout_sec=180)
    assert out["started"] is True
    # running 标志在返回瞬间可能已被快速替身复位（竞态），只断言已受理

    deadline = time.monotonic() + 5
    while svc.image_key_status()["recheck_running"]:
        assert time.monotonic() < deadline, "后台捕获未在 5s 内结束"
        time.sleep(0.05)

    status = svc.image_key_status()
    assert status["recheck_running"] is False
    assert status["recheck_last_ok"] is True
    assert status["recheck_last_at"]
    assert status["recheck_last_error"] == ""
    assert dl.calls and dl.calls[0] == (True, 180.0)


def test_recheck_miss_records_error(monkeypatch):
    """未捕获到密钥：last_ok=False、last_error 用户可读、ready 仍为 False。"""
    dl = _RecheckDL(result=None)
    svc = _make_svc(monkeypatch, dl)

    svc.recheck_image_key(timeout_sec=30)
    deadline = time.monotonic() + 5
    while svc.image_key_status()["recheck_running"]:
        assert time.monotonic() < deadline
        time.sleep(0.05)

    status = svc.image_key_status()
    assert status["recheck_last_ok"] is False
    assert "未捕获" in status["recheck_last_error"]


def test_recheck_worker_exception_is_contained(monkeypatch):
    """detect_image_key 抛异常：线程收敛、last_ok=False、原因可见。"""
    dl = _RecheckDL(result=RuntimeError("scan boom"))
    svc = _make_svc(monkeypatch, dl)

    svc.recheck_image_key(timeout_sec=30)
    deadline = time.monotonic() + 5
    while svc.image_key_status()["recheck_running"]:
        assert time.monotonic() < deadline
        time.sleep(0.05)

    status = svc.image_key_status()
    assert status["recheck_last_ok"] is False
    assert "scan boom" in status["recheck_last_error"]


def test_recheck_reentry_rejected(monkeypatch):
    """扫描进行中再次点击：started=False（防重入），原扫描继续。"""
    dl = _RecheckDL(result=("0123456789abcdef", 0xf9), delay=0.4)
    svc = _make_svc(monkeypatch, dl)

    first = svc.recheck_image_key(timeout_sec=30)
    assert first["started"] is True
    second = svc.recheck_image_key(timeout_sec=30)
    assert second["started"] is False

    deadline = time.monotonic() + 5
    while svc.image_key_status()["recheck_running"]:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert len(dl.calls) == 1


def test_recheck_without_downloader(monkeypatch):
    """媒体下载器不可用：不抛异常，last_ok=False + 可读原因。"""
    svc = WeChatService(Database(":memory:"), lambda _u: None)
    monkeypatch.setattr(svc, "_get_media_downloader", lambda: None)

    svc.recheck_image_key(timeout_sec=30)
    deadline = time.monotonic() + 5
    while svc.image_key_status()["recheck_running"]:
        assert time.monotonic() < deadline
        time.sleep(0.05)

    status = svc.image_key_status()
    assert status["recheck_last_ok"] is False
    assert "不可用" in status["recheck_last_error"]


def test_image_key_status_ready_keeps_recheck_fields(monkeypatch):
    """密钥就绪时 recheck_* 字段仍然回显（finally 语义）。"""
    svc = WeChatService(Database(":memory:"), lambda _u: None)

    class _DL:
        _img_key = ("0123456789abcdef", 0xf9)

    monkeypatch.setattr(svc, "_get_media_downloader", lambda: _DL())
    status = svc.image_key_status()
    assert status["ready"] is True
    assert status["source"] == "memory"
    assert status["recheck_running"] is False
    assert status["recheck_last_ok"] is None


# ---------------------------------------------------------------------------
# 4) 路由：POST /api/system/image-key-recheck
# ---------------------------------------------------------------------------

class _Pipeline:
    queue_size = 0
    queue_max = 100
    dropped_total = 0
    busy = False
    analysis_health = {}


class _RouteSvc:
    """路由层替身：recheck_image_key / image_key_status 可编程。"""

    listener_running = True
    monitored_count = 1
    available = True
    watermark_saved_at = ""
    degraded_reason = ""

    def __init__(self, started=True, key_status=None):
        self.started = started
        self._key_status = key_status or {
            "ready": False, "source": "", "recheck_running": True,
            "recheck_last_ok": None, "recheck_last_at": "",
            "recheck_last_error": "",
        }

    def probe_wechat_window(self):
        return False

    def cache_status(self):
        return []

    def image_key_status(self):
        return dict(self._key_status)

    def recheck_image_key(self, timeout_sec=240):
        return {"started": self.started, "running": True}


@pytest.fixture()
def cleanup():
    yield
    deps.configure(db=None, wechat=None, pipeline=None, sender=None,
                   notifier=None)


def _client(tmp_path, svc):
    db = Database(str(tmp_path / "recheck.sqlite3"))
    deps.configure(db=db, wechat=svc, pipeline=_Pipeline(),
                   sender=None, notifier=Notifier())
    app = FastAPI()
    app.include_router(system_router)
    return TestClient(app), db


def test_recheck_endpoint_starts_background_scan(tmp_path, cleanup):
    """POST /api/system/image-key-recheck → 202 语义：立即返回当前状态。"""
    client, db = _client(tmp_path, _RouteSvc(started=True))
    try:
        resp = client.post("/api/system/image-key-recheck")
        assert resp.status_code == 200
        body = resp.json()
        assert body["recheck_running"] is True
        assert body["ready"] is False
        # 状态查询接口同样带 recheck 字段
        status = client.get("/api/system/image-key-status").json()
        assert status["recheck_running"] is True
    finally:
        db.close()


def test_recheck_endpoint_schema_defaults(tmp_path, cleanup):
    """旧替身缺 recheck 字段 → Pydantic 默认值兜底，接口不炸。"""
    client, db = _client(tmp_path, _RouteSvc(
        started=False,
        key_status={"ready": True, "source": "cache"}))
    try:
        resp = client.post("/api/system/image-key-recheck")
        assert resp.status_code == 200
        body = resp.json()
        assert body["recheck_running"] is False
        assert body["recheck_last_ok"] is None
        assert body["ready"] is True
    finally:
        db.close()


def test_image_key_status_out_defaults():
    """schema 默认值：老调用方（只传 ready/source）不破坏。"""
    out = ImageKeyStatusOut(ready=True, source="derived")
    assert out.recheck_running is False
    assert out.recheck_last_ok is None
    assert out.recheck_last_at == ""
    assert out.recheck_last_error == ""
