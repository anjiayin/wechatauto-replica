# -*- coding: utf-8 -*-
"""更新器纯逻辑守门：版本判定 / 验签 / 路径穿越防护。

updater.py 作为独立脚本用 importlib 加载（它位于 packaging/，不在 app 包内）。
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import update_core

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
    """忽略绑定 sequence：老记录过期自愈；同 sequence 屏蔽；重发布解除。"""
    # 老 DB 只有版本号没有 sequence → 视为过期，存量客户端无需清库即可自愈。
    # （本断言相对旧语义是有意为之的变更，理由同上。）
    assert updater.decide("1.2.1", _manifest(), "1.2.2", 0) == "optional"
    # 新格式：忽略时 manifest.sequence=17，原样重查仍屏蔽。
    assert updater.decide("1.2.1", _manifest(), "1.2.2", 0, "stable", 17) == "none"
    # 同版本号重发布，sequence 抬高 → 自动解除忽略。
    assert updater.decide("1.2.1", _manifest(seq=18), "1.2.2", 0,
                          "stable", 17) == "optional"
    # 忽略的是别的版本号时不屏蔽。
    assert updater.decide("1.2.1", _manifest(), "1.2.1", 0, "stable", 17) == "optional"
    # 强制更新不受忽略影响。
    assert updater.decide("1.1.9", _manifest(min_sup="1.2.0"), "1.2.2", 0,
                          "stable", 17) == "forced"


def test_parse_ignored_sequence_handles_legacy_and_invalid(updater):
    assert updater.parse_ignored_sequence("") is None
    assert updater.parse_ignored_sequence(None) is None
    assert updater.parse_ignored_sequence("abc") is None
    assert updater.parse_ignored_sequence(" 1250 ") == 1250


def test_updater_ignored_sequence_db_roundtrip(updater, monkeypatch, tmp_path):
    """托盘链路：忽略 sequence 落库、读取、重发布判定与清理全链路。"""
    import sqlite3
    monkeypatch.setattr(updater, "DATA", tmp_path)
    con = sqlite3.connect(tmp_path / "kefu.sqlite3")
    con.execute("create table settings(key text primary key, value text)")
    con.commit()
    con.close()

    assert updater._ignored_sequence() is None        # 老库无该键
    updater._save_state("1.2.5", 0, 1250)
    assert updater._state() == ("1.2.5", 0)
    assert updater._ignored_sequence() == 1250

    m = {"channel": "stable", "sequence": 1250, "latest": "1.2.5",
         "min_supported": "0.0.0"}
    seq = updater._ignored_sequence()
    assert updater.decide("1.2.4", m, "1.2.5", 0, "stable", seq) == "none"
    republished = dict(m, sequence=1251)
    assert updater.decide("1.2.4", republished, "1.2.5", 0,
                          "stable", seq) == "optional"

    updater._save_state("", 1251)                    # 升级成功后清理
    assert updater._state() == ("", 1251)
    assert updater._ignored_sequence() is None


def test_pick_package(updater):
    m = {"packages": [
        {"type": "app", "version": "1.2.2", "from_versions": ["1.2.1"], "url": "app"},
        {"type": "full", "version": "1.2.2", "url": "full"},
    ]}
    assert updater.pick_package(m, "1.2.1")["type"] == "app"
    assert updater.pick_package(m, "1.0.0")["type"] == "full"


# ---------------------------------------------------------------------------
# full 全量安装包（Inno Setup exe）静默重装
# ---------------------------------------------------------------------------
def test_is_full_package():
    assert update_core.is_full_package(None) is False
    assert update_core.is_full_package({}) is False
    assert update_core.is_full_package({"type": "app"}) is False
    assert update_core.is_full_package({"type": "full"}) is True
    assert update_core.is_full_package({"type": "FULL"}) is True
    assert update_core.is_full_package({"type": " Full "}) is True


def test_full_install_args(tmp_path):
    # /FORCECLOSEAPPLICATIONS 是现场 1.3.6 拍出来的必需项：本程序无窗口消息循环，
    # RestartManager 关不掉 → Abort/Retry 框被 /SUPPRESSMSGBOXES 自动答 Abort → 无声回滚。
    assert update_core.full_install_args(Path("full-1.2.2.exe")) == [
        "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES",
        "/CLOSEAPPLICATIONS", "/FORCECLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS",
    ]
    short = update_core.full_install_args(Path("full-1.2.2.exe"),
                                          close_apps=False)
    assert short == ["/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"]
    assert "/CLOSEAPPLICATIONS" not in short
    assert "/FORCECLOSEAPPLICATIONS" not in short
    assert "/RESTARTAPPLICATIONS" not in short
    # /LOG 由调用方决定是否带上：静默安装失败本来就无声，不留痕就无法取证
    log = tmp_path / "i.log"
    logged = update_core.full_install_args(Path("full-1.2.2.exe"), log_path=log)
    assert logged[-1] == f"/LOG={log}"
    exe = tmp_path / "data" / "update" / "full-1.2.2.exe"
    assert update_core.full_install_log_path(exe) == exe.with_suffix(".log")


def test_launch_full_installer_rejects_missing_exe(monkeypatch, tmp_path, caplog):
    calls = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            calls.append((cmd, kwargs))

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    with caplog.at_level("WARNING"):
        assert update_core.launch_full_installer(tmp_path / "missing.exe") is False
    assert calls == []
    assert "安装包不存在" in caplog.text


def test_launch_full_installer_is_detached_and_never_waits(monkeypatch, tmp_path,
                                                           caplog):
    exe = tmp_path / "full-1.2.2.exe"
    exe.write_bytes(b"MZ fake installer")
    calls = []

    class FakePopen:
        """记录调用；故意不提供可正常返回的 wait/communicate。"""

        pid = 4242

        def __init__(self, cmd, **kwargs):
            calls.append({"cmd": list(cmd), "kwargs": dict(kwargs)})

        def wait(self, *a, **k):
            raise AssertionError("full 安装器绝不能 wait()")

        def communicate(self, *a, **k):
            raise AssertionError("full 安装器绝不能 communicate()")

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    with caplog.at_level("INFO"):
        assert update_core.launch_full_installer(exe) is True
    assert len(calls) == 1

    cmd = calls[0]["cmd"]
    assert cmd == [
        str(exe), "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES",
        "/CLOSEAPPLICATIONS", "/FORCECLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS",
        f"/LOG={update_core.full_install_log_path(exe)}",
    ]
    kwargs = calls[0]["kwargs"]
    assert kwargs["close_fds"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["creationflags"] != 0
    assert "cwd" not in kwargs                      # 沿用安装包默认目录，不传 /DIR
    # 拉起即留痕：exe / PID / 安装器日志路径（Popen 成功 ≠ 安装成功）
    text = caplog.text
    assert "4242" in text and str(exe) in text
    assert str(update_core.full_install_log_path(exe)) in text


def test_launch_full_installer_swallows_popen_error(monkeypatch, tmp_path, caplog):
    exe = tmp_path / "boom.exe"
    exe.write_bytes(b"MZ")

    def boom(*args, **kwargs):
        raise OSError("CreateProcess failed")

    monkeypatch.setattr(subprocess, "Popen", boom)
    with caplog.at_level("WARNING"):
        assert update_core.launch_full_installer(exe) is False
    assert "CreateProcess failed" in caplog.text


def _full_manifest(version: str = "1.2.2", seq: int = 17,
                   blob: bytes = b"MZ fake") -> dict:
    return {
        "channel": "stable", "sequence": seq, "latest": version,
        "min_supported": "0.0.0", "notes": "full 安装测试",
        "packages": [{"type": "full", "version": version, "url": "u",
                      "sha256": hashlib.sha256(blob).hexdigest(),
                      "size": len(blob)}],
    }


def _seed_bundle_for_full(tmp_path, version: str = "1.2.2") -> Path:
    bundle = tmp_path / "bundle"
    (bundle / "app" / "1.2.1" / "kefu-system" / "app").mkdir(parents=True)
    (bundle / "current.json").write_text(
        json.dumps({"version": "1.2.1", "active": "1.2.1"}), encoding="utf-8")
    return bundle


def test_updater_run_full_package_launches_installer(
        updater, monkeypatch, tmp_path, capsys):
    """托盘链路：full 包落盘 .exe、detached 拉起，不解压/不切 junction。

    关键回归：拉起安装器后只记 pending，**绝不**写 last_sequence——否则
    安装被杀软拦停时客户端再也不会提示升级（1.3.2 事故）。
    """
    bundle = _seed_bundle_for_full(tmp_path)
    exe = bundle / "data" / "update" / "full-1.2.2.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ fake")

    junction_calls = _run_env(updater, monkeypatch, bundle)
    monkeypatch.setattr(updater, "_fetch_manifest",
                        lambda: (_full_manifest(), None, ""))
    saved, pending_saved = [], []
    monkeypatch.setattr(updater, "_save_state",
                        lambda *a, **k: saved.append((a, k)))
    monkeypatch.setattr(updater, "_save_pending",
                        lambda *a, **k: pending_saved.append((a, k)))
    launched = []
    monkeypatch.setattr(updater, "launch_full_installer",
                        lambda p: launched.append(Path(p)) or True)

    assert updater.run() == 0
    assert launched == [exe]
    assert saved == []
    assert pending_saved == [(("1.2.2", 17), {})]
    assert junction_calls == []
    assert json.loads((bundle / "current.json").read_text(
        encoding="utf-8"))["version"] == "1.2.1"
    assert not (bundle / "app" / "1.2.2").exists()
    # 托盘没有 kefu.log，安装器日志路径必须打在 stdout 上供现场取证
    assert str(update_core.full_install_log_path(exe)) in capsys.readouterr().out


def test_updater_run_full_package_launch_failure_returns_8(
        updater, monkeypatch, tmp_path, capsys):
    """拉起失败返回新增码 8，且绝不推进状态/切 junction。"""
    bundle = _seed_bundle_for_full(tmp_path)
    exe = bundle / "data" / "update" / "full-1.2.2.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ fake")

    junction_calls = _run_env(updater, monkeypatch, bundle)
    monkeypatch.setattr(updater, "_fetch_manifest",
                        lambda: (_full_manifest(), None, ""))
    saved, pending_saved = [], []
    monkeypatch.setattr(updater, "_save_state",
                        lambda *a, **k: saved.append((a, k)))
    monkeypatch.setattr(updater, "_save_pending",
                        lambda *a, **k: pending_saved.append((a, k)))
    monkeypatch.setattr(updater, "launch_full_installer", lambda p: False)

    assert updater.run() == 8
    assert "全量安装启动失败" in capsys.readouterr().out
    assert saved == []
    assert pending_saved == []
    assert junction_calls == []
    assert json.loads((bundle / "current.json").read_text(
        encoding="utf-8"))["version"] == "1.2.1"


class _FakeUpdateDB:
    """UpdateService 所需的最小 settings DAO 替身。"""

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


def _svc_ready_for_full(tmp_path, *, blob: bytes = b"MZ fake",
                        sha: str | None = None):
    """构造「full 包已下载完成、等待 apply」的后端服务现场。"""
    from app import update_service as us

    bundle = _seed_bundle_for_full(tmp_path)
    exe = bundle / "data" / "update" / "full-1.2.2.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(blob)
    manifest = _full_manifest(blob=blob)
    if sha is not None:
        manifest["packages"][0]["sha256"] = sha

    svc = us.UpdateService(_FakeUpdateDB())
    svc._manifest = lambda: (manifest, "")            # type: ignore[method-assign]
    svc._last_manifest = manifest
    svc._last_check_sequence = 17
    with svc._lock:
        svc._job = {"state": "done", "done": len(blob), "total": len(blob),
                    "message": "下载完成", "version": "1.2.2",
                    "action": "optional", "package_type": "full"}
    return us, svc, bundle, exe


def test_update_service_full_apply_launches_installer(monkeypatch, tmp_path,
                                                       caplog):
    """后端链路：full 包复核通过后拉起安装器，不动 current.json / junction。

    关键点：sequence 只在启动自检确认新版本后推进；此刻只写 pending。
    """
    us, svc, bundle, exe = _svc_ready_for_full(tmp_path)
    monkeypatch.setattr(us, "bundle_root", lambda: bundle)
    launched = []
    monkeypatch.setattr(us.core, "launch_full_installer",
                        lambda p: launched.append(p) or True)

    with caplog.at_level("INFO"):
        code, payload = svc.apply()
    assert code == 200, payload
    assert payload == {"ok": True, "need_restart": True, "version": "1.2.2",
                       "full_install": True, "install_pending": True}
    assert launched == [exe]
    assert json.loads((bundle / "current.json").read_text(
        encoding="utf-8"))["version"] == "1.2.1"
    assert not (bundle / "app" / "1.2.2").exists()
    assert update_core.KEY_LAST_SEQUENCE not in svc.db.data
    assert svc.db.data[update_core.KEY_PENDING_VERSION] == "1.2.2"
    assert svc.db.data[update_core.KEY_PENDING_SEQUENCE] == "17"
    assert svc.db.data[update_core.KEY_IGNORED_VERSION] == ""
    assert svc.status()["state"] == "installing"
    assert svc.status()["message"] == "全量安装已启动，等待新版本启动确认"
    # 现场取证入口：复核结果与安装器日志路径都要落进 kefu.log
    assert "安装包复核通过" in caplog.text
    assert str(update_core.full_install_log_path(exe)) in caplog.text


def test_reconcile_full_install_confirms_only_after_new_version_starts(
        monkeypatch, tmp_path, caplog):
    """启动自检：版本没变→保留 pending 不推进；版本已升→推进并清 pending。"""
    from app import update_service as us

    monkeypatch.setattr(us.core, "launch_full_installer", lambda p: True)
    _us, svc, bundle, exe = _svc_ready_for_full(tmp_path)
    monkeypatch.setattr(us, "bundle_root", lambda: bundle)
    svc.apply()                                  # 拉起安装器，只写 pending
    db = svc.db
    assert db.data[update_core.KEY_PENDING_VERSION] == "1.2.2"
    assert update_core.KEY_LAST_SEQUENCE not in db.data

    # 安装器被拦停：进程还停在 1.2.1 → 不能推进 sequence，继续提示。
    # WARN 里要给出安装器日志路径，现场不用再猜去哪取证。
    with caplog.at_level("WARNING"):
        assert us.reconcile_full_install(db, "1.2.1") == "pending"
    assert str(update_core.full_install_log_path(exe)) in caplog.text
    assert update_core.KEY_LAST_SEQUENCE not in db.data
    assert db.data[update_core.KEY_PENDING_VERSION] == "1.2.2"

    # 新版本起来了 → 确认落地，推进 sequence 并清掉 pending。
    assert us.reconcile_full_install(db, "1.2.2") == "confirmed"
    assert db.data[update_core.KEY_LAST_SEQUENCE] == "17"
    assert db.data[update_core.KEY_PENDING_VERSION] == ""
    assert db.data[update_core.KEY_PENDING_SEQUENCE] == ""
    assert us.reconcile_full_install(db, "1.2.2") == "none"


def test_reconcile_never_regresses_sequence(monkeypatch, tmp_path):
    """已推进过更高 sequence 时，旧 pending 不得把 last_sequence 往回写。"""
    from app import update_service as us

    db = _FakeUpdateDB()
    db.data[update_core.KEY_LAST_SEQUENCE] = "20"
    db.data[update_core.KEY_PENDING_VERSION] = "1.2.2"
    db.data[update_core.KEY_PENDING_SEQUENCE] = "17"
    assert us.reconcile_full_install(db, "1.2.2") == "confirmed"
    assert db.data[update_core.KEY_LAST_SEQUENCE] == "20"


def test_reconcile_log_names_pending_not_only_current(monkeypatch, tmp_path,
                                                       caplog):
    """确认日志要写清「被满足的是哪个在途版本」，不能只报当前运行版本。

    现场 1.3.7 那轮走的是 1.2MB app 增量，日志却打出「全量安装 1.3.7 已确认
    生效」——那行只打印 current_version，而真正被清掉的是 1.3.6 遗留的
    pending，复盘时把一次增量升级误读成成功的全量升级。
    """
    from app import update_service as us

    db = _FakeUpdateDB()
    db.data[update_core.KEY_PENDING_VERSION] = "1.2.2"
    db.data[update_core.KEY_PENDING_SEQUENCE] = "17"
    with caplog.at_level("INFO"):
        assert us.reconcile_full_install(db, "1.2.3") == "confirmed"
    assert "1.2.2" in caplog.text                 # 被满足的 pending
    assert "1.2.3" in caplog.text                 # 当前运行版本
    assert db.data[update_core.KEY_LAST_SEQUENCE] == "17"


def test_check_keeps_prompting_when_full_install_pending(monkeypatch, tmp_path):
    """旧版客户端提前推进过 sequence 时，pending 仍要保留重试提示。"""
    from app import update_service as us

    monkeypatch.setattr(us.core, "launch_full_installer", lambda p: True)
    _us, svc, bundle, _exe = _svc_ready_for_full(tmp_path)
    monkeypatch.setattr(us, "bundle_root", lambda: bundle)
    svc.apply()
    svc.db.data[update_core.KEY_LAST_SEQUENCE] = "17"   # 模拟旧版提前推进
    monkeypatch.setattr(us, "_manifest_url", "https://example.invalid/stable")
    monkeypatch.setattr(us, "_channel", "stable")
    monkeypatch.setattr(us, "APP_VERSION", "1.2.1")
    monkeypatch.setattr(svc, "_manifest",
                        lambda: (_full_manifest(), ""))  # type: ignore[method-assign]
    result = svc.check()
    assert result["action"] == "optional"
    assert result["retry"] is True


def test_update_service_full_apply_retryable_when_launch_failed(
        monkeypatch, tmp_path):
    """安装器没拉起来：保留已校验包与 done 状态，可直接重试安装。"""
    us, svc, bundle, _exe = _svc_ready_for_full(tmp_path)
    monkeypatch.setattr(us, "bundle_root", lambda: bundle)
    monkeypatch.setattr(us.core, "launch_full_installer", lambda p: False)

    code, payload = svc.apply()
    assert code == 500 and "可重试安装" in payload["detail"]
    assert svc.status()["state"] == "done"
    assert update_core.KEY_LAST_SEQUENCE not in svc.db.data
    assert update_core.KEY_PENDING_VERSION not in svc.db.data
    assert (bundle / "data" / "update" / "full-1.2.2.exe").is_file()


def test_update_service_full_apply_aborts_on_sha_mismatch(monkeypatch, tmp_path):
    """二次验签后的 sha256 复核失败 → 停在原版本，绝不拉起安装器。"""
    us, svc, bundle, _exe = _svc_ready_for_full(tmp_path, sha="0" * 64)
    monkeypatch.setattr(us, "bundle_root", lambda: bundle)
    launched = []
    monkeypatch.setattr(us.core, "launch_full_installer",
                        lambda p: launched.append(p) or True)

    code, payload = svc.apply()
    assert code == 500 and "SHA256" in payload["detail"]
    assert launched == []
    assert svc.status()["state"] == "error"
    assert update_core.KEY_LAST_SEQUENCE not in svc.db.data
    assert json.loads((bundle / "current.json").read_text(
        encoding="utf-8"))["version"] == "1.2.1"


# ---- 重启后本地全量包补位（免重新下载） ----
def _restart_service(svc) -> None:
    """模拟进程重启：内存状态机与缓存的 manifest 全部清零。"""
    with svc._lock:
        svc._job = {"state": "idle", "done": 0, "total": 0, "message": "",
                    "version": "", "action": "none"}
    svc._last_manifest = None


def test_status_hydrates_unconfirmed_local_full_package(monkeypatch, tmp_path,
                                                         caplog):
    """全量安装被拦停 → 重启服务 → 本地包还在，状态直接补成「已下载」。

    现场投诉：手工重启服务也触发不了全量升级，界面只剩「重新下载」，用户得再
    下一遍几百 MB。补位后前端现成的 state==='done' 分支给出「安装并重启」。
    """
    us, svc, bundle, exe = _svc_ready_for_full(tmp_path)
    monkeypatch.setattr(us, "bundle_root", lambda: bundle)
    monkeypatch.setattr(us.core, "launch_full_installer", lambda p: True)
    monkeypatch.setattr(us, "APP_VERSION", "1.2.1")
    svc.apply()                                  # 只写 pending，安装器没落地

    _restart_service(svc)
    with caplog.at_level("INFO"):
        st = svc.status()
    assert st["state"] == "done"
    assert st["version"] == "1.2.2"
    assert st["package_type"] == "full"
    assert st["done"] == st["total"] == exe.stat().st_size
    assert "本地安装包" in caplog.text
    # 补位只改显示状态：绝不推进 sequence、绝不提前清 pending
    assert update_core.KEY_LAST_SEQUENCE not in svc.db.data
    assert svc.db.data[update_core.KEY_PENDING_VERSION] == "1.2.2"


def test_status_hydrate_skips_empty_missing_and_confirmed(monkeypatch, tmp_path):
    """空文件 / 包不存在 / 版本已生效：一律不补位，别把用户误导成「可安装」。"""
    us, svc, bundle, exe = _svc_ready_for_full(tmp_path)
    monkeypatch.setattr(us, "bundle_root", lambda: bundle)
    monkeypatch.setattr(us.core, "launch_full_installer", lambda p: True)
    monkeypatch.setattr(us, "APP_VERSION", "1.2.1")
    svc.apply()

    exe.write_bytes(b"")                          # 0 字节 = 半途失败，不算有包
    _restart_service(svc)
    assert svc.status()["state"] == "idle"

    exe.write_bytes(b"MZ fake")                   # 新版本已经起来了，等启动自检
    monkeypatch.setattr(us, "APP_VERSION", "1.2.2")
    _restart_service(svc)
    assert svc.status()["state"] == "idle"

    monkeypatch.setattr(us, "APP_VERSION", "1.2.1")
    exe.unlink()                                  # data 被清理过 → 只能重新下载
    _restart_service(svc)
    assert svc.status()["state"] == "idle"


def test_status_hydrate_respects_busy_state_and_dev_layout(monkeypatch, tmp_path):
    """进行中的下载/安装不被覆盖；开发态（无安装根）不补位也不报错。"""
    from app import update_service as us_mod

    monkeypatch.setattr(us_mod, "bundle_root", lambda: None)
    db = _FakeUpdateDB()
    db.data[update_core.KEY_PENDING_VERSION] = "1.2.2"
    svc = us_mod.UpdateService(db)

    with svc._lock:
        svc._job = {"state": "downloading", "done": 1, "total": 9, "message": "",
                    "version": "1.2.2", "action": "optional",
                    "package_type": "full"}
    assert svc.status()["state"] == "downloading"

    _restart_service(svc)
    assert svc.status()["state"] == "idle"


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


def test_full_install_confirmed_uses_numeric_version_compare():
    """版本确认必须用数字元组：1.10 > 1.9，且空/非法 pending 一律未确认。"""
    from app.update_core import full_install_confirmed

    assert full_install_confirmed("1.10.0", "1.9.0") is True
    assert full_install_confirmed("1.9.0", "1.10.0") is False
    assert full_install_confirmed("1.3.3", "1.3.3") is True
    assert full_install_confirmed("1.3.3", "not-a-version") is False
    assert full_install_confirmed("1.3.3", "") is False
