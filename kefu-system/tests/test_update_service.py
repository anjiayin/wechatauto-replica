# -*- coding: utf-8 -*-
"""后端更新接口守门：未确认不下载、未下载不切换、判定与托盘共用、授权白名单。"""
from __future__ import annotations

import io
import json
import logging
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from app import update_service as us
from app.api import license_guard
from app.api.router_update import router as update_router
from app.update_core import (
    KEY_IGNORED_SEQUENCE, KEY_IGNORED_VERSION, KEY_LAST_SEQUENCE,
)


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
    # 忽略记录必须绑定当时 manifest 的 sequence（本 fixture 为 17）
    assert svc.db.data[KEY_IGNORED_SEQUENCE] == "17"
    out = svc.check()
    assert out["action"] == "none"
    assert out["ignored"] is True
    assert out["latest"] == "9.9.9"                 # notes/latest 仍可展示


def test_legacy_ignored_record_without_sequence_self_heals(svc):
    """存量客户端现场：只有 version 没有 sequence → 老记录过期，无需清库。"""
    svc.db.data[KEY_IGNORED_VERSION] = "9.9.9"
    assert KEY_IGNORED_SEQUENCE not in svc.db.data
    out = svc.check()
    assert out["action"] == "optional"
    assert out["ignored"] is False


def test_field_scenario_legacy_ignored_1250_self_heal(monkeypatch):
    """现场复现：current=1.2.4、DB ignored=1.2.5/last_sequence=0、
    manifest latest=1.2.5 seq=1250 → 新代码无需清库即可显示更新。"""
    monkeypatch.setattr(us, "_manifest_url", "http://example.invalid/kefu")
    monkeypatch.setattr(us, "APP_VERSION", "1.2.4")
    db = FakeDB()
    db.data[KEY_IGNORED_VERSION] = "1.2.5"
    db.data[KEY_LAST_SEQUENCE] = "0"
    s = us.UpdateService(db)
    s._manifest = lambda: (_manifest(latest="1.2.5", seq=1250), "")

    out = s.check()
    assert out["action"] == "optional"
    assert out["ignored"] is False
    assert out["latest"] == "1.2.5" and out["sequence"] == 1250


def test_republished_same_version_higher_sequence_restores_prompt(svc):
    """同版本号 1.2.5 -> sequence 1250 重发布：忽略自动解除。"""
    assert svc.check()["action"] == "optional"
    svc.ignore("9.9.9")
    assert svc.check()["action"] == "none"

    republished = _manifest(latest="9.9.9", seq=18)
    svc._manifest = lambda: (republished, "")
    out = svc.check()
    assert out["action"] == "optional"
    assert out["ignored"] is False
    assert out["sequence"] == 18


def test_same_sequence_keeps_ignore(svc):
    """未重发布（sequence 不变）时忽略继续生效，不打扰用户。"""
    svc.check()
    svc.ignore("9.9.9")
    for _ in range(2):
        out = svc.check()
        assert out["action"] == "none" and out["ignored"] is True


def test_unignore_restores_prompt(svc):
    """取消忽略接口让用户自行恢复，不必手改 DB。"""
    svc.check()
    svc.ignore("9.9.9")
    assert svc.check()["ignored"] is True

    assert svc.unignore() == {"ok": True, "ignored": ""}
    assert svc.db.data[KEY_IGNORED_VERSION] == ""
    assert svc.db.data[KEY_IGNORED_SEQUENCE] == ""
    assert svc.check()["action"] == "optional"


def test_forced_update_is_not_suppressed_by_ignore(svc):
    """低于 min_supported 的客户端即使点过忽略也必须强制更新。"""
    forced = _manifest(latest="9.9.9", seq=18)
    forced["min_supported"] = "99.0.0"
    svc._manifest = lambda: (forced, "")
    assert svc.check()["action"] == "forced"
    svc.ignore("9.9.9")
    assert svc.db.data[KEY_IGNORED_SEQUENCE] == "18"
    assert svc.check()["action"] == "forced"


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
    # 升级成功后必须同时清掉忽略记录（含 sequence），避免残留脏状态
    svc.db.data[KEY_IGNORED_VERSION] = "1.2.2"
    svc.db.data[KEY_IGNORED_SEQUENCE] = "17"

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
    assert svc.db.data[KEY_IGNORED_VERSION] == ""
    assert svc.db.data[KEY_IGNORED_SEQUENCE] == ""


def test_update_routes_registered():
    paths = {r.path for r in update_router.routes}
    assert paths == {
        "/api/system/update/check", "/api/system/update/download",
        "/api/system/update/status", "/api/system/update/apply",
        "/api/system/update/ignore", "/api/system/update/unignore",
    }


# ---------------------------------------------------------------------------
# bundle_root：安装根 launcher.pyc / updater.pyc / start_kefu.bat 更新
# ---------------------------------------------------------------------------
def _zip_with_bundle_root(zip_path: Path, *, with_root: bool = True,
                          extra_root: str | None = None) -> None:
    """构造真实 zip：版本层 + 可选 bundle_root/ 前缀（可注入白名单外文件）。"""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("kefu-system/app/marker.pyc", b"v-new")
        if with_root:
            zf.writestr("bundle_root/launcher.pyc", b"LAUNCHER-NEW")
            zf.writestr("bundle_root/updater.pyc", b"UPDATER-NEW")
            zf.writestr("bundle_root/start_kefu.bat", b"START-BAT-NEW")
        if extra_root:
            zf.writestr(f"bundle_root/{extra_root}", b"EVIL")
    zip_path.write_bytes(buf.getvalue())


def _install_old_bundle_root(bundle: Path) -> None:
    """在安装根放旧版白名单文件，验证升级后确实被替换。"""
    (bundle / "launcher.pyc").write_bytes(b"LAUNCHER-OLD")
    (bundle / "updater.pyc").write_bytes(b"UPDATER-OLD")


def test_apply_installs_bundle_root_files(monkeypatch, svc, tmp_path):
    """含 bundle_root/ 的 zip：安装根三个白名单文件被更新，junction 照常切换。"""
    bundle = tmp_path / "bundle"
    _make_current_junction(bundle, "1.2.1")
    _install_old_bundle_root(bundle)
    (bundle / "current.json").write_text(
        json.dumps({"version": "1.2.1", "active": "1.2.1"}),
        encoding="utf-8")
    _zip_with_bundle_root(bundle / "data" / "update" / "app-1.2.2.zip")
    monkeypatch.setattr(us, "bundle_root", lambda: bundle)
    _prepare_done_job(svc, "1.2.2")

    code, payload = svc.apply()
    assert code == 200, payload
    assert (bundle / "launcher.pyc").read_bytes() == b"LAUNCHER-NEW"
    assert (bundle / "updater.pyc").read_bytes() == b"UPDATER-NEW"
    assert (bundle / "start_kefu.bat").read_bytes() == b"START-BAT-NEW"
    # 版本层与 junction 照常切换
    assert (bundle / "app" / "1.2.2" / "kefu-system" / "app" / "marker.pyc").is_file()
    assert (bundle / "app" / "current").resolve() == \
        (bundle / "app" / "1.2.2").resolve()
    assert json.loads((bundle / "current.json").read_text(
        encoding="utf-8"))["version"] == "1.2.2"
    assert svc.status()["state"] == "applied"


def test_apply_old_style_zip_leaves_bundle_root_untouched(
        monkeypatch, svc, tmp_path):
    """向后兼容：zip 无 bundle_root/ 时行为与从前完全一致，安装根不动。"""
    bundle = tmp_path / "bundle"
    _make_current_junction(bundle, "1.2.1")
    _install_old_bundle_root(bundle)
    (bundle / "current.json").write_text(
        json.dumps({"version": "1.2.1", "active": "1.2.1"}),
        encoding="utf-8")
    _zip_with_bundle_root(bundle / "data" / "update" / "app-1.2.2.zip",
                          with_root=False)
    monkeypatch.setattr(us, "bundle_root", lambda: bundle)
    _prepare_done_job(svc, "1.2.2")

    code, _ = svc.apply()
    assert code == 200
    assert (bundle / "launcher.pyc").read_bytes() == b"LAUNCHER-OLD"
    assert (bundle / "updater.pyc").read_bytes() == b"UPDATER-OLD"
    assert not (bundle / "start_kefu.bat").exists()
    assert json.loads((bundle / "current.json").read_text(
        encoding="utf-8"))["version"] == "1.2.2"


def test_apply_aborts_on_nonwhitelist_bundle_root_entry(
        monkeypatch, svc, tmp_path):
    """zip 投毒第二道闸：bundle_root 白名单外条目 → 中止，系统零改动。"""
    bundle = tmp_path / "bundle"
    _make_current_junction(bundle, "1.2.1")
    _install_old_bundle_root(bundle)
    (bundle / "current.json").write_text(
        json.dumps({"version": "1.2.1", "active": "1.2.1"}),
        encoding="utf-8")
    _zip_with_bundle_root(bundle / "data" / "update" / "app-1.2.2.zip",
                          extra_root="evil.exe")
    monkeypatch.setattr(us, "bundle_root", lambda: bundle)
    _prepare_done_job(svc, "1.2.2")

    code, payload = svc.apply()
    assert code == 500 and "白名单外" in payload["detail"]
    assert svc.status()["state"] == "error"
    # 不切 junction、不写 current.json、安装根旧文件完好
    assert json.loads((bundle / "current.json").read_text(
        encoding="utf-8"))["version"] == "1.2.1"
    assert (bundle / "app" / "current").resolve() == \
        (bundle / "app" / "1.2.1").resolve()
    assert (bundle / "launcher.pyc").read_bytes() == b"LAUNCHER-OLD"
    # target 已清理，不留半成品
    assert not (bundle / "app" / "1.2.2").exists()


def test_apply_degrades_to_skip_when_root_file_locked(
        monkeypatch, svc, tmp_path, caplog):
    """根文件替换持续失败（如杀软占用）→ 重试 3 次后跳过该文件，升级继续。"""
    bundle = tmp_path / "bundle"
    _make_current_junction(bundle, "1.2.1")
    _install_old_bundle_root(bundle)
    (bundle / "current.json").write_text(
        json.dumps({"version": "1.2.1", "active": "1.2.1"}),
        encoding="utf-8")
    _zip_with_bundle_root(bundle / "data" / "update" / "app-1.2.2.zip")
    monkeypatch.setattr(us, "bundle_root", lambda: bundle)
    _prepare_done_job(svc, "1.2.2")

    real_copyfile = shutil.copyfile
    attempts = {"launcher.pyc": 0}

    def flaky_copyfile(src, dst, **kwargs):
        if Path(dst).name == "launcher.pyc":
            attempts["launcher.pyc"] += 1
            raise PermissionError(32, "进程无法访问，文件正被另一进程使用")
        return real_copyfile(src, dst, **kwargs)

    monkeypatch.setattr(shutil, "copyfile", flaky_copyfile)
    monkeypatch.setattr(us.time, "sleep", lambda *_: None)

    with caplog.at_level(logging.WARNING, logger="app.update_service"):
        code, _ = svc.apply()
    assert code == 200                                    # 升级不被卡死
    assert attempts["launcher.pyc"] == us._ROOT_COPY_ATTEMPTS
    assert (bundle / "launcher.pyc").read_bytes() == b"LAUNCHER-OLD"  # 跳过
    assert (bundle / "updater.pyc").read_bytes() == b"UPDATER-NEW"    # 其余照常
    assert (bundle / "start_kefu.bat").read_bytes() == b"START-BAT-NEW"
    assert json.loads((bundle / "current.json").read_text(
        encoding="utf-8"))["version"] == "1.2.2"
    assert any("跳过该文件" in r.message for r in caplog.records)


def test_license_guard_allows_update_endpoints():
    for p in ("/api/system/update/check", "/api/system/update/download",
              "/api/system/update/status", "/api/system/update/apply",
              "/api/system/update/ignore", "/api/system/update/unignore"):
        assert license_guard._allowed(p) is True


# ---------------------------------------------------------------------------
# 启动自检补写（首跳缺口）：旧 apply 解压遗留的版本层 bundle_root/ 补写安装根
# ---------------------------------------------------------------------------
def _seed_leftover_bundle_root(bundle: Path, version: str = "1.2.4") -> Path:
    """构造「旧 apply 遗留」现场：版本层 bundle_root/ + 安装根旧文件。

    返回版本层目录（repair 通过 APP_ROOT 的父目录定位它）。
    """
    version_layer = bundle / "app" / version
    (version_layer / "kefu-system" / "app").mkdir(parents=True, exist_ok=True)
    root = version_layer / "bundle_root"
    root.mkdir(parents=True, exist_ok=True)
    (root / "launcher.pyc").write_bytes(b"LAUNCHER-NEW")
    (root / "updater.pyc").write_bytes(b"UPDATER-NEW")
    (root / "start_kefu.bat").write_bytes(b"START-BAT-NEW")
    (bundle / "launcher.pyc").write_bytes(b"LAUNCHER-OLD")
    (bundle / "updater.pyc").write_bytes(b"UPDATER-OLD")
    return version_layer


def test_startup_repair_backfills_bundle_root_files(
        monkeypatch, tmp_path, caplog):
    """版本层有遗留 bundle_root/ 且安装根是旧内容 → 补写为新内容并记 INFO。"""
    bundle = tmp_path / "bundle"
    version_layer = _seed_leftover_bundle_root(bundle)
    monkeypatch.setattr(us, "APP_ROOT", version_layer / "kefu-system")
    monkeypatch.setattr(us, "bundle_root", lambda: bundle)

    with caplog.at_level(logging.INFO, logger="app.update_service"):
        updated = us.repair_bundle_root_files()

    assert set(updated) == {"launcher.pyc", "updater.pyc", "start_kefu.bat"}
    assert (bundle / "launcher.pyc").read_bytes() == b"LAUNCHER-NEW"
    assert (bundle / "updater.pyc").read_bytes() == b"UPDATER-NEW"
    assert (bundle / "start_kefu.bat").read_bytes() == b"START-BAT-NEW"
    info = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert any("补写到安装根" in m and "launcher.pyc" in m for m in info)


def test_startup_repair_is_idempotent(monkeypatch, tmp_path, caplog):
    """每次启动都跑：内容已一致时不再报「更新」，文件内容保持不变。"""
    bundle = tmp_path / "bundle"
    version_layer = _seed_leftover_bundle_root(bundle)
    monkeypatch.setattr(us, "APP_ROOT", version_layer / "kefu-system")
    monkeypatch.setattr(us, "bundle_root", lambda: bundle)

    us.repair_bundle_root_files()
    caplog.clear()                             # 只看第二次（幂等）调用的日志
    with caplog.at_level(logging.INFO, logger="app.update_service"):
        updated = us.repair_bundle_root_files()

    assert updated == []                       # 内容一致 → 不再列出更新文件
    assert (bundle / "launcher.pyc").read_bytes() == b"LAUNCHER-NEW"
    assert (bundle / "updater.pyc").read_bytes() == b"UPDATER-NEW"
    assert (bundle / "start_kefu.bat").read_bytes() == b"START-BAT-NEW"
    assert not any("补写到安装根" in r.message for r in caplog.records)


def test_startup_repair_noop_without_leftover(monkeypatch, tmp_path):
    """版本层无 bundle_root/（正常升级路径或全新安装）→ 无动作不报错。"""
    bundle = tmp_path / "bundle"
    version_layer = bundle / "app" / "1.2.4"
    (version_layer / "kefu-system" / "app").mkdir(parents=True)
    (bundle / "launcher.pyc").write_bytes(b"LAUNCHER-OLD")
    monkeypatch.setattr(us, "APP_ROOT", version_layer / "kefu-system")
    monkeypatch.setattr(us, "bundle_root", lambda: bundle)

    assert us.repair_bundle_root_files() == []
    assert (bundle / "launcher.pyc").read_bytes() == b"LAUNCHER-OLD"
    assert not (bundle / "start_kefu.bat").exists()


def test_startup_repair_skips_nonwhitelist_entry(
        monkeypatch, tmp_path, caplog):
    """遗留目录被塞入白名单外文件 → WARN 不阻断启动，且任何文件都不写入。"""
    bundle = tmp_path / "bundle"
    version_layer = _seed_leftover_bundle_root(bundle)
    (version_layer / "bundle_root" / "evil.exe").write_bytes(b"EVIL")
    monkeypatch.setattr(us, "APP_ROOT", version_layer / "kefu-system")
    monkeypatch.setattr(us, "bundle_root", lambda: bundle)

    with caplog.at_level(logging.WARNING, logger="app.update_service"):
        updated = us.repair_bundle_root_files()

    assert updated == []                       # fail-open：返回空列表不抛异常
    # 拒收发生在任何写入之前：安装根旧文件原样保留
    assert (bundle / "launcher.pyc").read_bytes() == b"LAUNCHER-OLD"
    assert (bundle / "updater.pyc").read_bytes() == b"UPDATER-OLD"
    assert not (bundle / "start_kefu.bat").exists()
    assert not (bundle / "evil.exe").exists()
    assert any("白名单外" in r.message for r in caplog.records)


def test_startup_repair_noop_in_dev_mode(monkeypatch, tmp_path):
    """开发态（bundle_root() 返回 None）→ 直接 no-op，绝不碰源码树。"""
    version_layer = tmp_path / "app" / "1.2.4"
    (version_layer / "kefu-system").mkdir(parents=True)
    (version_layer / "bundle_root").mkdir()
    (version_layer / "bundle_root" / "launcher.pyc").write_bytes(b"X")
    monkeypatch.delenv("KEFU_BUNDLE_ROOT", raising=False)
    monkeypatch.setattr(us, "APP_ROOT", version_layer / "kefu-system")

    assert us.repair_bundle_root_files() == []
    assert not (version_layer / "launcher.pyc").exists()


def test_startup_repair_noop_when_current_junction_layout(
        monkeypatch, tmp_path):
    """经 app/current junction 启动（abspath 不跟随链接）也能定位版本层。"""
    bundle = tmp_path / "bundle"
    version_layer = _seed_leftover_bundle_root(bundle)
    # 不建真实 junction：APP_ROOT 用字面 current 路径即可覆盖判定逻辑
    monkeypatch.setattr(us, "APP_ROOT",
                        bundle / "app" / "current" / "kefu-system")
    monkeypatch.setattr(us, "bundle_root", lambda: bundle)

    updated = us.repair_bundle_root_files()
    # 字面 current 下没有 bundle_root/ → no-op（真实 junction 场景由
    # mklink 用例覆盖；这里锁定「不会误写到 app/ 层」的底线）
    assert updated == []
    assert (bundle / "launcher.pyc").read_bytes() == b"LAUNCHER-OLD"


def test_startup_repair_through_real_junction(monkeypatch, tmp_path):
    """发布态主路径：APP_ROOT 经 app/current junction → 补写真实版本层遗留。"""
    bundle = tmp_path / "bundle"
    version_layer = _seed_leftover_bundle_root(bundle)
    link = _make_current_junction(bundle, "1.2.4")
    monkeypatch.setattr(us, "APP_ROOT", link / "kefu-system")
    monkeypatch.setattr(us, "bundle_root", lambda: bundle)

    updated = us.repair_bundle_root_files()
    assert set(updated) == {"launcher.pyc", "updater.pyc", "start_kefu.bat"}
    assert (bundle / "launcher.pyc").read_bytes() == b"LAUNCHER-NEW"
    assert (bundle / "start_kefu.bat").read_bytes() == b"START-BAT-NEW"
