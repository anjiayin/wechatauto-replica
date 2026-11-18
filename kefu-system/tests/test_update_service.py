# -*- coding: utf-8 -*-
"""后端更新接口守门：未确认不下载、未下载不切换、判定与托盘共用、授权白名单。"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

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


# ---------------------------------------------------------------------------
# bundle_root：junction 启动场景（1.2.1 -> 1.2.2 升级被 409 拦住的回归用例）
# ---------------------------------------------------------------------------
def _make_current_junction(bundle: Path, version: str) -> Path:
    """建真实 app/current junction；平台不支持时跳过。"""
    if sys.platform != "win32":
        pytest.skip("junction 用例仅 Windows 有意义")
    target = bundle / "app" / version
    (target / "kefu-system" / "app").mkdir(parents=True, exist_ok=True)
    link = bundle / "app" / "current"
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.decode("mbcs", errors="replace").strip()
        pytest.skip(f"mklink /J 不可用（需权限）：{detail}")
    return link


def test_bundle_root_with_real_junction(monkeypatch, tmp_path):
    """经 junction 启动的安装版必须返回安装根，而不是误判为开发态。"""
    bundle = tmp_path / "bundle"
    _make_current_junction(bundle, "1.2.1")
    app_root = bundle / "app" / "current" / "kefu-system"

    # 实证根因：resolve() 跟随 junction，parent.name 变成版本号而非 "current"。
    assert app_root.resolve().parent.name == "1.2.1"
    assert app_root.parent.name == "current"

    monkeypatch.delenv("KEFU_BUNDLE_ROOT", raising=False)
    monkeypatch.setattr(us, "APP_ROOT", app_root)
    assert us.bundle_root() == bundle


def test_bundle_root_uses_literal_current_without_junction(monkeypatch, tmp_path):
    """无 junction 权限时也要覆盖「未 resolve 路径判定」这条主逻辑。"""
    app_root = tmp_path / "app" / "current" / "kefu-system"
    app_root.mkdir(parents=True)
    monkeypatch.delenv("KEFU_BUNDLE_ROOT", raising=False)
    monkeypatch.setattr(us, "APP_ROOT", app_root)
    assert us.bundle_root() == tmp_path


@pytest.mark.parametrize("relative", [
    "kefu-system",                       # 开发态（仓库/kefu-system）
    "app/1.2.1/kefu-system",             # 直接按版本目录启动，没有 current 层
    "notapp/current/kefu-system",        # 结构相似但不是 P1 布局
])
def test_bundle_root_fails_closed_outside_p1(monkeypatch, tmp_path, relative):
    """开发态/旧 P0/非法布局一律 None——放宽判定不能削弱 fail-closed。"""
    monkeypatch.delenv("KEFU_BUNDLE_ROOT", raising=False)
    monkeypatch.setattr(us, "APP_ROOT", tmp_path / Path(relative))
    assert us.bundle_root() is None


def test_bundle_root_explicit_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("KEFU_BUNDLE_ROOT", str(tmp_path))
    monkeypatch.setattr(us, "APP_ROOT", tmp_path / "kefu-system")
    assert us.bundle_root() == tmp_path


def _prepare_done_job(svc, version: str) -> None:
    """把服务置于「已校验下载完成」，并让二次 manifest 校验匹配该版本。"""
    manifest = _manifest(latest=version, seq=17)
    svc._last_manifest = manifest
    svc._manifest = lambda: (manifest, "")
    with svc._lock:
        svc._job = {"state": "done", "done": 1024, "total": 1024,
                    "message": "下载完成", "version": version, "action": "optional"}


def test_download_fails_closed_when_not_installed(svc, monkeypatch):
    assert svc.check()["action"] == "optional"
    monkeypatch.setattr(us, "bundle_root", lambda: None)
    code, payload = svc.download(True)
    assert code == 409 and "不是客户端安装版" in payload["detail"]
    assert svc.status()["state"] == "idle"


def test_apply_fails_closed_when_not_installed(svc, monkeypatch):
    _prepare_done_job(svc, "1.2.2")
    monkeypatch.setattr(us, "bundle_root", lambda: None)
    code, payload = svc.apply()
    assert code == 409 and "不是客户端安装版" in payload["detail"]
    assert svc.status()["state"] == "done"      # 未进入 applying，原状态保持不变


def test_download_installed_targets_bundle_data_dir(monkeypatch, svc, tmp_path):
    """安装态下载落点必须是 bundle/data/update，而不是版本层目录。"""
    svc.check()
    captured: dict[str, Path] = {}

    def fake_download(url, dst, sha, size, progress=None):
        captured["dst"] = Path(dst)
        if progress:
            progress(size, size)
        return True, ""

    class SyncThread:
        """让后台下载线程同步执行，断言不受调度影响。"""

        def __init__(self, target=None, **kwargs) -> None:
            self._target = target

        def start(self) -> None:
            assert self._target is not None
            self._target()

    monkeypatch.setattr(us.core, "download", fake_download)
    monkeypatch.setattr(us.threading, "Thread", SyncThread)
    monkeypatch.setattr(us, "bundle_root", lambda: tmp_path)

    code, payload = svc.download(True)
    assert code == 202 and payload["version"] == "9.9.9"
    assert captured["dst"] == tmp_path / "data" / "update" / "app-9.9.9.zip"
    assert svc.status()["state"] == "done"


def test_apply_cross_version_switches_junction_and_current_json(
        monkeypatch, svc, tmp_path):
    """1.2.1 -> 1.2.2 全流程：解压到 app/1.2.2、切 junction、写 current.json。"""
    bundle = tmp_path / "bundle"
    _make_current_junction(bundle, "1.2.1")
    (bundle / "current.json").write_text(
        json.dumps({"version": "1.2.1", "active": "1.2.1", "previous": "1.2.0"}),
        encoding="utf-8")

    # 后端下载阶段真实落点：bundle/data/update/app-1.2.2.zip
    zip_path = bundle / "data" / "update" / "app-1.2.2.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("kefu-system/app/marker.pyc", b"v122")
        zf.writestr("kefu-system/version.txt", "1.2.2")
    zip_path.write_bytes(buf.getvalue())

    monkeypatch.setattr(us, "bundle_root", lambda: bundle)
    _prepare_done_job(svc, "1.2.2")

    code, payload = svc.apply()
    assert code == 200, payload

    target = bundle / "app" / "1.2.2"
    assert (target / "kefu-system" / "app" / "marker.pyc").is_file()
    assert (bundle / "app" / "current").resolve() == target.resolve()
    assert (bundle / "app" / "current" / "kefu-system" / "app"
            / "marker.pyc").is_file()
    assert json.loads((bundle / "current.json").read_text(encoding="utf-8")) == {
        "version": "1.2.2", "active": "1.2.2", "previous": "1.2.1"}
    assert svc.status()["state"] == "applied"


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
