# -*- coding: utf-8 -*-
"""安装器场景守门：安装包不含 junction，启动器按 current.json 补建。"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
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


def test_child_env_injects_bundle_root(launcher):
    """后端与托盘须拿到同一个权威安装根，避免 junction resolve 误判。"""
    env = launcher._child_env()
    assert env["KEFU_BUNDLE_ROOT"] == str(launcher.BUNDLE)


def test_child_env_injects_update_config(launcher, monkeypatch, tmp_path):
    """bundle 根 update_config.json 须注入更新地址/渠道；空或非法地址不注入。"""
    cfg = tmp_path / "update_config.json"
    monkeypatch.setattr(launcher, "UPDATE_CONFIG", cfg)
    cfg.write_text(json.dumps({"manifest_url": "https://cdn.example.com/kefu/stable/",
                               "channel": "beta"}), encoding="utf-8")
    env = launcher._child_env()
    assert env["KEFU_UPDATE_MANIFEST_URL"] == "https://cdn.example.com/kefu/stable"
    assert env["KEFU_UPDATE_CHANNEL"] == "beta"

    # 空地址 fail-safe：不注入 manifest url
    cfg.write_text(json.dumps({"manifest_url": "", "channel": "stable"}), encoding="utf-8")
    env2 = launcher._child_env()
    assert "KEFU_UPDATE_MANIFEST_URL" not in env2
    assert env2["KEFU_UPDATE_CHANNEL"] == "stable"

    # 非法地址（非 http）不注入；文件损坏也不崩
    cfg.write_text(json.dumps({"manifest_url": "file:///etc/passwd"}), encoding="utf-8")
    assert "KEFU_UPDATE_MANIFEST_URL" not in launcher._child_env()
    cfg.write_text("{broken json", encoding="utf-8")
    assert "KEFU_UPDATE_MANIFEST_URL" not in launcher._child_env()


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
                 "version.txt", "app.ico", "delivery_seed.json", "update_config.json"):
        assert name in txt, f"ISS 未打包 {name}"
    # 安装包与快捷方式都用 app.ico
    assert "SetupIconFile=" in txt
    assert "IconFilename:" in txt


# ---------------------------------------------------------------------------
# 端口解析：必须与后端 run.py 的「环境变量 > 配置库 > 默认」优先级一致
# ---------------------------------------------------------------------------
@pytest.fixture
def port_data_dir(launcher, monkeypatch, tmp_path: Path) -> Path:
    """隔离端口来源：清掉环境变量，把 launcher 的数据根指向临时目录。"""
    monkeypatch.delenv("KEFU_PORT", raising=False)
    monkeypatch.delenv("KEFU_DATA_DIR", raising=False)
    monkeypatch.setattr(launcher, "DATA", tmp_path)
    return tmp_path


def _make_settings_db(directory: Path, value: object = None,
                      create_table: bool = True) -> Path:
    """造一个最小配置库；value=None 表示只建表不写 run.port。"""
    db = directory / "kefu.sqlite3"
    with sqlite3.connect(db) as conn:
        if create_table:
            conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
            if value is not None:
                conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)",
                             ("run.port", str(value)))
    return db


def test_read_port_uses_saved_port_from_db(launcher, port_data_dir):
    """用户在设置页保存 run.port=8766 后，启动器应探测/打开 8766。"""
    db = _make_settings_db(port_data_dir, 8766)
    before = db.read_bytes()
    assert launcher._read_port() == 8766
    assert db.read_bytes() == before, "只读查询不得改写配置库"
    # 不能把配置库端口塞进 KEFU_PORT 抬高后端优先级
    assert "KEFU_PORT" not in launcher._child_env()


def test_read_port_env_overrides_saved_port(launcher, port_data_dir, monkeypatch):
    """环境变量仍优先于配置库（与后端优先级一致）。"""
    _make_settings_db(port_data_dir, 8766)
    monkeypatch.setenv("KEFU_PORT", "8770")
    assert launcher._read_port() == 8770


def test_read_port_prefers_kefu_data_dir_override(launcher, port_data_dir, monkeypatch):
    """配置库目录按 KEFU_DATA_DIR 解析（与 app.config.DB_PATH 一致）。"""
    custom = port_data_dir / "custom-data"
    custom.mkdir()
    _make_settings_db(custom, 8777)
    monkeypatch.setenv("KEFU_DATA_DIR", str(custom))
    assert launcher._read_port() == 8777


def test_read_port_falls_back_on_missing_db_or_table(launcher, port_data_dir):
    """首次安装无配置库、或库里还没有 settings 表 → 回退 8765 且不建库。"""
    assert launcher._read_port() == 8765
    assert not (port_data_dir / "kefu.sqlite3").exists()
    _make_settings_db(port_data_dir, create_table=False)
    assert launcher._read_port() == 8765


@pytest.mark.parametrize("bad_value", ["", "abc", "0", "-1", "65536", "12.5", None])
def test_read_port_falls_back_on_invalid_saved_value(launcher, port_data_dir,
                                                      bad_value):
    """配置值损坏/非法不得让启动器崩溃，回退默认端口。"""
    if bad_value is None:               # NULL 值：建表后插入 NULL
        db = port_data_dir / "kefu.sqlite3"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO settings (key, value) VALUES (?, NULL)",
                         ("run.port",))
    else:
        _make_settings_db(port_data_dir, bad_value)
    assert launcher._read_port() == 8765


@pytest.mark.parametrize("bad_env", ["", "abc", "0", "65536", "12.5"])
def test_read_port_falls_back_on_invalid_env(launcher, port_data_dir, monkeypatch,
                                              bad_env):
    """非法环境变量同样 fail-safe，并可继续落到配置库/默认值。"""
    monkeypatch.setenv("KEFU_PORT", bad_env)
    assert launcher._read_port() == 8765
    _make_settings_db(port_data_dir, 8766)
    assert launcher._read_port() == 8766, "非法 env 应被忽略，而不是压住配置库"


def test_probe_and_open_ui_use_resolved_port(launcher, monkeypatch):
    """就绪探测与打开界面必须都用 _state['port'] 解析出的真实端口。"""
    monkeypatch.setitem(launcher._state, "port", 8766)
    monkeypatch.setitem(launcher._state, "opened", False)
    monkeypatch.setitem(launcher._state, "proc", None)

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    probed: list[str] = []

    def fake_urlopen(url: str, timeout: float):
        probed.append(url)
        return _Resp()

    monkeypatch.setattr(launcher.urllib.request, "urlopen", fake_urlopen)
    assert launcher._port_ready(launcher._state["port"], timeout=1.0) is True
    assert probed == ["http://127.0.0.1:8766/api/status"]

    opened: list[str] = []
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    launcher._open_ui()
    assert opened == ["http://127.0.0.1:8766/"]
    assert launcher._state["opened"] is True


# ---------------------------------------------------------------------------
# 单实例互斥拦截：必须弹 MessageBox 给用户可见反馈，不能无声退出
# ---------------------------------------------------------------------------
def test_main_shows_messagebox_when_already_running(launcher, monkeypatch):
    """mutex 拿不到时 main() 须弹提示框，且绝不启动后端、正常返回不抛异常。"""
    monkeypatch.setattr(launcher, "_acquire_single_instance", lambda: None)
    calls: list[tuple[int, str, str, int]] = []

    def fake_messagebox(hwnd, text, title, flags):
        calls.append((hwnd, text, title, flags))
        return 0

    monkeypatch.setattr(launcher.win32gui, "MessageBox", fake_messagebox)
    started: list[bool] = []
    monkeypatch.setattr(launcher, "_start_backend",
                        lambda: started.append(True) or None)

    launcher.main()                                  # 不得抛异常
    assert len(calls) == 1
    hwnd, text, title, flags = calls[0]
    assert hwnd == 0
    assert title == "微信客服"
    assert "已在运行" in text
    assert "托盘" in text
    assert "退出" in text
    assert flags == (launcher.win32con.MB_OK | launcher.win32con.MB_ICONINFORMATION)
    assert started == [], "已在运行分支不得启动后端"


def test_already_running_messagebox_failsafe(launcher, monkeypatch):
    """MessageBox 弹框失败（无窗口站等）不得让启动器抛异常，静默继续退出路径。"""
    def broken_messagebox(hwnd, text, title, flags):
        raise OSError("no window station")

    monkeypatch.setattr(launcher.win32gui, "MessageBox", broken_messagebox)
    launcher._show_already_running_message()         # 不抛异常即通过


def test_main_no_messagebox_when_mutex_acquired(launcher, monkeypatch):
    """正常拿到 mutex 时不得弹「已在运行」提示框。"""
    monkeypatch.setattr(launcher, "_acquire_single_instance",
                        lambda: object())            # 假句柄
    calls: list[tuple] = []
    monkeypatch.setattr(launcher, "_show_already_running_message",
                        lambda *a, **kw: calls.append((a, kw)))
    # short-circuit：version 层校验失败，main 在弹框之后立即返回
    monkeypatch.setattr(launcher, "_ensure_current", lambda: False)

    launcher.main()
    assert calls == []
