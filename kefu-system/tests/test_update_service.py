# -*- coding: utf-8 -*-
"""后端更新接口守门：未确认不下载、未下载不切换、判定与托盘共用、授权白名单。"""
from __future__ import annotations

import pytest

from app import update_service as us
from app.api import license_guard
from app.api.router_update import router as update_router
from app.update_core import KEY_IGNORED_VERSION, KEY_LAST_SEQUENCE


class FakeDB:
    """UpdateService 只依赖 get_setting/get_int/set_setting 三个方法。"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get_setting(self, key: str, default: str = "") -> str:
        return self.data.get(key, default)

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.data.get(key, str(default)))
        except ValueError:
            return default

    def set_setting(self, key: str, value: str) -> None:
        self.data[key] = str(value)


def _manifest(latest: str = "9.9.9", seq: int = 17) -> dict:
    return {
        "channel": "stable", "sequence": seq, "latest": latest,
        "min_supported": "0.0.0", "notes": "修复若干问题",
        "requires_antivirus_off": True,
        "packages": [{"type": "app", "version": latest, "size": 1024,
                      "sha256": "abc", "url": "http://example.invalid/app.zip"}],
    }


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setattr(us, "_manifest_url", "http://example.invalid/kefu")
    s = us.UpdateService(FakeDB())
    monkeypatch.setattr(s, "_manifest", lambda: (_manifest(), ""))
    return s


def test_check_returns_public_summary(svc):
    out = svc.check()
    assert out["ok"] is True
    assert out["action"] == "optional"
    assert out["latest"] == "9.9.9"
    assert out["notes"] == "修复若干问题"
    assert out["requires_antivirus_off"] is True
    assert out["package_size"] == 1024
    # 内部字段不回前端
    assert "package_sha256" not in out
    assert "package_url" not in out
    assert "key_id" not in out


def test_check_without_manifest_url_is_silent(monkeypatch):
    monkeypatch.setattr(us, "_manifest_url", "")
    s = us.UpdateService(FakeDB())
    out = s.check()
    assert out["action"] == "none"
    assert "未配置" in out["reason"]


def test_ignore_makes_check_return_none(svc):
    assert svc.check()["action"] == "optional"
    svc.ignore("9.9.9")
    assert svc.db.data[KEY_IGNORED_VERSION] == "9.9.9"
    assert svc.check()["action"] == "none"


def test_sequence_replay_guard(svc):
    svc.db.data[KEY_LAST_SEQUENCE] = "17"
    assert svc.check()["action"] == "none"


def test_download_requires_antivirus_confirmation(svc):
    svc.check()
    code, payload = svc.download(False)
    assert code == 400 and "关闭安全软件" in payload["detail"]
    assert svc.status()["state"] == "idle"


def test_download_without_check_is_rejected(svc):
    code, payload = svc.download(True)
    assert code == 409


def test_apply_requires_completed_download(svc):
    code, payload = svc.apply()
    assert code == 409 and "请先下载" in payload["detail"]


def test_bundle_root_returns_none_in_dev(monkeypatch):
    monkeypatch.delenv("KEFU_BUNDLE_ROOT", raising=False)
    assert us.bundle_root() is None


def test_update_routes_registered():
    paths = {r.path for r in update_router.routes}
    assert paths == {
        "/api/system/update/check", "/api/system/update/download",
        "/api/system/update/status", "/api/system/update/apply",
        "/api/system/update/ignore",
    }


def test_license_guard_allows_update_endpoints():
    for p in ("/api/system/update/check", "/api/system/update/download",
              "/api/system/update/status", "/api/system/update/apply"):
        assert license_guard._allowed(p) is True
