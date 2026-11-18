# -*- coding: utf-8 -*-
"""更新器纯逻辑守门：版本判定 / 验签 / 路径穿越防护。

updater.py 作为独立脚本用 importlib 加载（它位于 packaging/，不在 app 包内）。
"""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

UPDATER = Path(__file__).resolve().parents[2] / "packaging" / "updater.py"


@pytest.fixture(scope="module")
def updater():
    spec = importlib.util.spec_from_file_location("p1_updater", UPDATER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# ---- 安装根定位（与后端 update_service.bundle_root 同一优先级） ----
def test_resolve_bundle_explicit_wins(updater, monkeypatch, tmp_path):
    monkeypatch.delenv("KEFU_BUNDLE_ROOT", raising=False)
    assert updater.resolve_bundle(str(tmp_path)) == tmp_path
    assert updater.resolve_bundle("   ") == UPDATER.parent


def test_resolve_bundle_from_environment(updater, monkeypatch, tmp_path):
    monkeypatch.setenv("KEFU_BUNDLE_ROOT", str(tmp_path))
    assert updater.resolve_bundle() == tmp_path
    assert updater.resolve_bundle("") == tmp_path


# ---- 版本比较 ----
def test_version_numeric_order(updater):
    assert updater.compare_versions("1.9", "1.10") < 0
    assert updater.compare_versions("1.2.1", "1.2.2") < 0
    assert updater.compare_versions("1.3.0", "1.3.0") == 0
    assert updater.compare_versions("2.0.0", "1.9.9") > 0


def test_version_prerelease(updater):
    assert updater.compare_versions("1.3.0-beta", "1.3.0") < 0
    assert updater.compare_versions("1.3.0-rc.1", "1.3.0-beta") > 0


# ---- decide 判定规则 ----
def _manifest(latest="1.2.2", seq=17, channel="stable", min_sup="1.2.0"):
    return {"channel": channel, "sequence": seq, "latest": latest,
            "min_supported": min_sup, "notes": "test"}


def test_decide_no_update_when_current_is_latest(updater):
    assert updater.decide("1.2.2", _manifest(), "", 0) == "none"
    assert updater.decide("1.3.0", _manifest(), "", 0) == "none"  # 绝不降级


def test_decide_optional_and_forced(updater):
    assert updater.decide("1.2.1", _manifest(), "", 0) == "optional"
    assert updater.decide("1.1.9", _manifest(), "", 0) == "forced"  # < min_supported


def test_decide_channel_and_replay_guard(updater):
    assert updater.decide("1.2.1", _manifest(channel="beta"), "", 0) == "none"
    assert updater.decide("1.2.1", _manifest(seq=17), "", 17) == "none"
    assert updater.decide("1.2.1", _manifest(seq=17), "", 18) == "none"


def test_decide_ignored_version(updater):
    assert updater.decide("1.2.1", _manifest(), "1.2.2", 0) == "none"
    assert updater.decide("1.2.1", _manifest(), "1.2.1", 0) == "optional"


def test_pick_package(updater):
    m = {"packages": [
        {"type": "app", "version": "1.2.2", "from_versions": ["1.2.1"], "url": "app"},
        {"type": "full", "version": "1.2.2", "url": "full"},
    ]}
    assert updater.pick_package(m, "1.2.1")["type"] == "app"
    assert updater.pick_package(m, "1.0.0")["type"] == "full"


# ---- 验签 ----
def test_verify_manifest_ok_and_bad(updater):
    priv = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(
        priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    payload = b'{"latest":"1.2.2"}'
    sig = base64.b64encode(priv.sign(payload)).decode()
    keys = {"k1": pub_b64}
    assert updater.verify_manifest(payload, {"key_id": "k1", "signature": sig}, keys)[0]
    assert not updater.verify_manifest(payload + b" ", {"key_id": "k1", "signature": sig}, keys)[0]
    assert not updater.verify_manifest(payload, {"key_id": "unknown", "signature": sig}, keys)[0]


# ---- 路径穿越 ----
def test_safe_extract_blocks_traversal(updater, tmp_path):
    zpath = tmp_path / "evil.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../evil.txt", "pwned")
        zf.writestr("kefu-system/app/ok.pyc", b"ok")
    zpath.write_bytes(buf.getvalue())
    target = tmp_path / "out"
    ok, err = updater.safe_extract(zpath, target)
    assert not ok and "穿越" in err
    assert not (tmp_path.parent / "evil.txt").exists()


def test_safe_extract_normal(updater, tmp_path):
    zpath = tmp_path / "ok.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("kefu-system/app/x.pyc", b"ok")
    target = tmp_path / "out"
    ok, err = updater.safe_extract(zpath, target)
    assert ok and (target / "kefu-system" / "app" / "x.pyc").is_file()


# ---------------------------------------------------------------------------
# bundle_root：安装根 launcher.pyc / updater.pyc / start_kefu.bat 更新
# （updater 自带实现，与 app/update_service / packaging/build_release 同语义）
# ---------------------------------------------------------------------------
def test_root_bundle_whitelist_consistent(updater):
    """三处白名单（build_release / update_service / updater）必须一字不差。"""
    import importlib.util
    import sys
    from app import update_service as us
    spec = importlib.util.spec_from_file_location(
        "build_release_whitelist_check",
        Path(__file__).resolve().parents[2] / "packaging" / "build_release.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    expected = ("launcher.pyc", "updater.pyc", "start_kefu.bat")
    assert updater.ROOT_BUNDLE_FILES == expected
    assert us.ROOT_BUNDLE_FILES == expected
    assert mod.ROOT_BUNDLE_FILES == expected
    assert updater.BUNDLE_ROOT_DIR == us.BUNDLE_ROOT_DIR \
        == mod.BUNDLE_ROOT_DIR == "bundle_root"


def _make_target_with_bundle_root(target: Path, *, extra: str | None = None,
                                  omit: tuple[str, ...] = ()) -> None:
    root = target / "bundle_root"
    root.mkdir(parents=True)
    for name, blob in (("launcher.pyc", b"LAUNCHER-NEW"),
                       ("updater.pyc", b"UPDATER-NEW"),
                       ("start_kefu.bat", b"BAT-NEW")):
        if name not in omit:
            (root / name).write_bytes(blob)
    if extra:
        (root / extra).write_bytes(b"EVIL")


def test_updater_install_bundle_root_files(updater, tmp_path):
    target = tmp_path / "extracted"
    _make_target_with_bundle_root(target)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "launcher.pyc").write_bytes(b"LAUNCHER-OLD")

    updater.install_bundle_root_files(target, bundle)
    assert (bundle / "launcher.pyc").read_bytes() == b"LAUNCHER-NEW"
    assert (bundle / "updater.pyc").read_bytes() == b"UPDATER-NEW"
    assert (bundle / "start_kefu.bat").read_bytes() == b"BAT-NEW"


def test_updater_install_no_bundle_root_and_missing_files(updater, tmp_path):
    """解压目录无 bundle_root/ → no-op；白名单文件缺失 → 跳过不失败。"""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "launcher.pyc").write_bytes(b"OLD")
    target = tmp_path / "t1"
    target.mkdir()
    updater.install_bundle_root_files(target, bundle)
    assert (bundle / "launcher.pyc").read_bytes() == b"OLD"

    target2 = tmp_path / "t2"
    _make_target_with_bundle_root(target2, omit=("updater.pyc",))
    updater.install_bundle_root_files(target2, bundle)     # 不抛异常
    assert (bundle / "launcher.pyc").read_bytes() == b"LAUNCHER-NEW"
    assert not (bundle / "updater.pyc").exists()
    assert (bundle / "start_kefu.bat").read_bytes() == b"BAT-NEW"


def test_updater_install_rejects_nonwhitelist(updater, tmp_path):
    target = tmp_path / "extracted"
    _make_target_with_bundle_root(target, extra="evil.exe")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "launcher.pyc").write_bytes(b"OLD")

    with pytest.raises(RuntimeError) as ei:
        updater.install_bundle_root_files(target, bundle)
    assert "白名单外" in str(ei.value)
    # 拒绝发生在任何写入之前
    assert (bundle / "launcher.pyc").read_bytes() == b"OLD"
    assert not (bundle / "updater.pyc").exists()


def test_updater_install_degrades_when_locked(
        updater, monkeypatch, tmp_path, capsys):
    """根文件被占用 → 重试 3 次后打 WARN 跳过该文件，其余照常写入。"""
    target = tmp_path / "extracted"
    _make_target_with_bundle_root(target)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "launcher.pyc").write_bytes(b"OLD")

    real_copyfile = updater.shutil.copyfile
    attempts = {"n": 0}

    def flaky_copyfile(src, dst, **kwargs):
        if Path(dst).name == "launcher.pyc":
            attempts["n"] += 1
            raise PermissionError(32, "locked by another process")
        return real_copyfile(src, dst, **kwargs)

    monkeypatch.setattr(updater.shutil, "copyfile", flaky_copyfile)
    monkeypatch.setattr(updater.time, "sleep", lambda *_: None)

    updater.install_bundle_root_files(target, bundle)      # 不抛异常
    assert attempts["n"] == updater._ROOT_COPY_ATTEMPTS
    assert (bundle / "launcher.pyc").read_bytes() == b"OLD"
    assert (bundle / "updater.pyc").read_bytes() == b"UPDATER-NEW"
    out = capsys.readouterr().out
    assert "[WARN]" in out and "跳过该文件" in out


def _fake_manifest(latest: str = "1.2.2") -> dict:
    return {"channel": "stable", "sequence": 17, "latest": latest,
            "min_supported": "0.0.0", "notes": "test",
            "packages": [{"type": "app", "version": latest, "url": "u",
                          "sha256": "0" * 64, "size": 0}]}


def _run_env(updater, monkeypatch, bundle: Path) -> None:
    """把 updater.run() 的模块级常量与外部依赖指到临时 bundle。"""
    appdir = bundle / "app"
    monkeypatch.setattr(updater, "BUNDLE", bundle)
    monkeypatch.setattr(updater, "DATA", bundle / "data")
    monkeypatch.setattr(updater, "CURRENT_JSON", bundle / "current.json")
    monkeypatch.setattr(updater, "APP_DIR", appdir)
    monkeypatch.setattr(updater, "_fetch_manifest",
                        lambda: (_fake_manifest(), None, ""))
    monkeypatch.setattr(updater, "_state", lambda: ("", 0))
    monkeypatch.setattr(updater, "_save_state", lambda *a, **k: None)
    monkeypatch.setattr(updater, "confirm_antivirus", lambda *a, **k: True)
    monkeypatch.setattr(updater, "download", lambda *a, **k: (True, ""))
    junction_calls: list[Path] = []
    monkeypatch.setattr(updater, "_rebuild_junction",
                        lambda link, target: junction_calls.append(target)
                        or True)
    return junction_calls


def test_updater_run_installs_bundle_root_files(
        updater, monkeypatch, tmp_path):
    """run() 全流程（真实 safe_extract）：bundle_root 白名单文件写入安装根。"""
    bundle = tmp_path / "bundle"
    (bundle / "app" / "1.2.1" / "kefu-system" / "app").mkdir(parents=True)
    (bundle / "current.json").write_text(
        json.dumps({"version": "1.2.1", "active": "1.2.1"}), encoding="utf-8")
    (bundle / "launcher.pyc").write_bytes(b"LAUNCHER-OLD")

    zpath = bundle / "data" / "update" / "app-1.2.2.zip"
    zpath.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("kefu-system/app/marker.pyc", b"new")
        zf.writestr("bundle_root/launcher.pyc", b"LAUNCHER-NEW")
        zf.writestr("bundle_root/updater.pyc", b"UPDATER-NEW")
    zpath.write_bytes(buf.getvalue())

    junction_calls = _run_env(updater, monkeypatch, bundle)
    code = updater.run()
    assert code == 0
    assert (bundle / "launcher.pyc").read_bytes() == b"LAUNCHER-NEW"
    assert (bundle / "updater.pyc").read_bytes() == b"UPDATER-NEW"
    # 安装根写入发生在切 junction 之前
    assert junction_calls == [bundle / "app" / "1.2.2"]
    assert json.loads((bundle / "current.json").read_text(
        encoding="utf-8"))["version"] == "1.2.2"


def test_updater_run_aborts_on_nonwhitelist_bundle_root(
        updater, monkeypatch, tmp_path):
    """run() 路径的白名单闸：中止返回 7，不切 junction、不写 current.json。"""
    bundle = tmp_path / "bundle"
    (bundle / "app" / "1.2.1" / "kefu-system" / "app").mkdir(parents=True)
    (bundle / "current.json").write_text(
        json.dumps({"version": "1.2.1", "active": "1.2.1"}), encoding="utf-8")
    (bundle / "launcher.pyc").write_bytes(b"LAUNCHER-OLD")

    zpath = bundle / "data" / "update" / "app-1.2.2.zip"
    zpath.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("kefu-system/app/marker.pyc", b"new")
        zf.writestr("bundle_root/launcher.pyc", b"LAUNCHER-NEW")
        zf.writestr("bundle_root/evil.exe", b"EVIL")
    zpath.write_bytes(buf.getvalue())

    junction_calls = _run_env(updater, monkeypatch, bundle)
    code = updater.run()
    assert code == 7
    assert junction_calls == []                            # 未切 junction
    assert json.loads((bundle / "current.json").read_text(
        encoding="utf-8"))["version"] == "1.2.1"           # 未写 current.json
    assert (bundle / "launcher.pyc").read_bytes() == b"LAUNCHER-OLD"
    assert not (bundle / "app" / "1.2.2").exists()         # target 已清理


# ---------------------------------------------------------------------------
# 入口自检补写（首跳缺口）：旧 apply 遗留的版本层 bundle_root/ 补写安装根
# ---------------------------------------------------------------------------
def _seed_leftover_bundle_root(bundle: Path, version: str = "1.2.4") -> Path:
    """构造「旧 apply 遗留」现场：版本层 bundle_root/ + 安装根旧文件。

    返回版本层目录。repair 由 current.json 的 version 定位它。
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
    (bundle / "current.json").write_text(
        json.dumps({"version": version, "active": version}), encoding="utf-8")
    return version_layer


def _run_env_for_repair(updater, monkeypatch, bundle: Path) -> None:
    """把 repair 用到的模块级常量指到临时 bundle（不搭完整 run 环境）。"""
    monkeypatch.setattr(updater, "BUNDLE", bundle)
    monkeypatch.setattr(updater, "APP_DIR", bundle / "app")


def test_updater_repair_backfills_bundle_root_files(
        updater, monkeypatch, tmp_path, capsys):
    """版本层有遗留 bundle_root/ 且安装根是旧内容 → 补写为新内容并打印。"""
    bundle = tmp_path / "bundle"
    _seed_leftover_bundle_root(bundle)
    _run_env_for_repair(updater, monkeypatch, bundle)

    updated = updater.repair_bundle_root_files()

    assert set(updated) == {"launcher.pyc", "updater.pyc", "start_kefu.bat"}
    assert (bundle / "launcher.pyc").read_bytes() == b"LAUNCHER-NEW"
    assert (bundle / "updater.pyc").read_bytes() == b"UPDATER-NEW"
    assert (bundle / "start_kefu.bat").read_bytes() == b"START-BAT-NEW"
    out = capsys.readouterr().out
    assert "补写到安装根" in out and "launcher.pyc" in out


def test_updater_repair_is_idempotent(updater, monkeypatch, tmp_path, capsys):
    """每次 run() 都跑：内容已一致时不再打印「更新」，文件保持不变。"""
    bundle = tmp_path / "bundle"
    _seed_leftover_bundle_root(bundle)
    _run_env_for_repair(updater, monkeypatch, bundle)

    updater.repair_bundle_root_files()
    capsys.readouterr()                        # 丢弃第一次（补写）调用的输出
    updated = updater.repair_bundle_root_files()

    assert updated == []
    assert (bundle / "launcher.pyc").read_bytes() == b"LAUNCHER-NEW"
    assert "补写到安装根" not in capsys.readouterr().out


def test_updater_repair_noop_without_leftover(updater, monkeypatch, tmp_path):
    """版本层无 bundle_root/（正常升级路径或全新安装）→ 无动作不报错。"""
    bundle = tmp_path / "bundle"
    (bundle / "app" / "1.2.4" / "kefu-system" / "app").mkdir(parents=True)
    (bundle / "current.json").write_text(
        json.dumps({"version": "1.2.4", "active": "1.2.4"}), encoding="utf-8")
    (bundle / "launcher.pyc").write_bytes(b"LAUNCHER-OLD")
    _run_env_for_repair(updater, monkeypatch, bundle)

    assert updater.repair_bundle_root_files() == []
    assert (bundle / "launcher.pyc").read_bytes() == b"LAUNCHER-OLD"
    assert not (bundle / "start_kefu.bat").exists()


def test_updater_repair_skips_nonwhitelist_entry(
        updater, monkeypatch, tmp_path, capsys):
    """遗留目录被塞入白名单外文件 → WARN 不阻断，且任何文件都不写入。"""
    bundle = tmp_path / "bundle"
    version_layer = _seed_leftover_bundle_root(bundle)
    (version_layer / "bundle_root" / "evil.exe").write_bytes(b"EVIL")
    _run_env_for_repair(updater, monkeypatch, bundle)

    updated = updater.repair_bundle_root_files()       # fail-open 不抛异常

    assert updated == []
    assert (bundle / "launcher.pyc").read_bytes() == b"LAUNCHER-OLD"
    assert (bundle / "updater.pyc").read_bytes() == b"UPDATER-OLD"
    assert not (bundle / "start_kefu.bat").exists()
    assert not (bundle / "evil.exe").exists()
    out = capsys.readouterr().out
    assert "[WARN]" in out and "白名单外" in out


def test_updater_repair_noop_without_current_json(
        updater, monkeypatch, tmp_path):
    """current.json 缺失 → 版本号 "?"，对应目录不存在 → 安全 no-op。"""
    bundle = tmp_path / "bundle"
    (bundle / "app").mkdir(parents=True)
    (bundle / "launcher.pyc").write_bytes(b"LAUNCHER-OLD")
    _run_env_for_repair(updater, monkeypatch, bundle)

    assert updater.repair_bundle_root_files() == []
    assert (bundle / "launcher.pyc").read_bytes() == b"LAUNCHER-OLD"


def test_updater_run_calls_repair_first(updater, monkeypatch, tmp_path):
    """run() 入口先自检：遗留 bundle_root/ 在网络检查之前就被补写。"""
    bundle = tmp_path / "bundle"
    _seed_leftover_bundle_root(bundle)
    junction_calls = _run_env(updater, monkeypatch, bundle)

    code = updater.run()
    assert code == 0
    # 自检在 manifest 检查前完成：即使本次无新版本，安装根也已被补写
    assert (bundle / "launcher.pyc").read_bytes() == b"LAUNCHER-NEW"
    assert (bundle / "start_kefu.bat").read_bytes() == b"START-BAT-NEW"
    assert junction_calls == []        # 无新版本 → 不切 junction
