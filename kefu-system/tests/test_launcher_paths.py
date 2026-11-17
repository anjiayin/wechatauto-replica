# -*- coding: utf-8 -*-
"""安装器场景守门：安装包不含 junction，启动器按 current.json 补建。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[2] / "packaging" / "launcher.py"


@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location("p1_launcher", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _setup_bundle(launcher, monkeypatch, tmp_path: Path, version: str = "1.2.3"):
    monkeypatch.setattr(launcher, "BUNDLE", tmp_path)
    monkeypatch.setattr(launcher, "CURRENT_JSON", tmp_path / "current.json")
    (tmp_path / "current.json").write_text(
        json.dumps({"version": version}), encoding="utf-8")
    calls: list[tuple[Path, Path]] = []

    def fake_rebuild(link: Path, target: Path):
        calls.append((link, target))
        return True, ""

    monkeypatch.setattr(launcher, "_rebuild_junction", fake_rebuild)
    return calls


def test_ensure_current_rebuilds_from_current_json(launcher, monkeypatch, tmp_path):
    calls = _setup_bundle(launcher, monkeypatch, tmp_path)
    run_pyc = tmp_path / "app" / "1.2.3" / "kefu-system" / "run.pyc"
    run_pyc.parent.mkdir(parents=True)
    run_pyc.write_bytes(b"")
    assert launcher._ensure_current() is True
    assert len(calls) == 1
    assert calls[0][0] == tmp_path / "app" / "current"
    assert calls[0][1] == tmp_path / "app" / "1.2.3"


def test_ensure_current_rejects_missing_version_dir(launcher, monkeypatch, tmp_path):
    calls = _setup_bundle(launcher, monkeypatch, tmp_path)
    assert launcher._ensure_current() is False
    assert calls == []


def test_ensure_current_rejects_invalid_version(launcher, monkeypatch, tmp_path):
    calls = _setup_bundle(launcher, monkeypatch, tmp_path, version="../evil")
    assert launcher._ensure_current() is False
    assert calls == []


def test_uses_pythonw_no_console(launcher):
    """启动器须用 pythonw（无黑窗口），日志走自建 GUI 窗口。"""
    assert launcher.PYTHON.name.lower() == "pythonw.exe"


def test_child_env_injects_delivery_seed(launcher, monkeypatch, tmp_path):
    """bundle 根有 delivery_seed.json 时须注入 KEFU_DELIVERY_SEED。"""
    monkeypatch.setattr(launcher, "SEED_JSON", tmp_path / "delivery_seed.json")
    (tmp_path / "delivery_seed.json").write_text("{}", encoding="utf-8")
    env = launcher._child_env()
    assert env["KEFU_DELIVERY_SEED"] == str(tmp_path / "delivery_seed.json")

    # 无 seed 文件则不注入
    (tmp_path / "delivery_seed.json").unlink()
    env2 = launcher._child_env()
    assert "KEFU_DELIVERY_SEED" not in env2


def test_log_menu_and_close_semantics(launcher):
    """托盘须有查看/隐藏日志菜单；日志窗口 WM_CLOSE 只隐藏不退进程。"""
    src = LAUNCHER.read_text(encoding="utf-8")
    assert "MENU_LOG" in src and "查看/隐藏日志" in src
    # WM_CLOSE 分支：ShowWindow(..., SW_HIDE) 且没有 PostQuitMessage
    close_block = src.split("if msg == win32con.WM_CLOSE:")[1].split("if msg ==")[0]
    assert "SW_HIDE" in close_block
    assert "PostQuitMessage" not in close_block
    # 日志窗口默认隐藏
    assert "ShowWindow(hwnd, win32con.SW_HIDE)   # 默认隐藏" in src
    # 只有托盘退出 MENU_EXIT 分支才真正停后端
    exit_block = src.split("elif cmd == MENU_EXIT:")[1].split("return 0")[0]
    assert "_stop_backend()" in exit_block


def test_iss_packages_all_root_assets():
    """ISS 的 [Files] 段须列出全部 bundle 根资源，防止 seed/图标漏装。"""
    iss = LAUNCHER.parent.parent / "installer" / "kefu.iss"
    txt = iss.read_text(encoding="utf-8")
    for name in ("launcher.pyc", "updater.pyc", "start_kefu.bat", "current.json",
                 "version.txt", "app.ico", "delivery_seed.json"):
        assert name in txt, f"ISS 未打包 {name}"
    # 安装包与快捷方式都用 app.ico
    assert "SetupIconFile=" in txt
    assert "IconFilename:" in txt
