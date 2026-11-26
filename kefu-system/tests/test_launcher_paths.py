# -*- coding: utf-8 -*-
"""安装器场景守门：安装包不含 junction，启动器按 current.json 补建。"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import threading
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


def test_iss_launch_entry_runs_in_silent_mode():
    """[Run] 启动项不得带 skipifsilent：静默全量升级装完要自动把程序拉起来。

    本机 ISCC 三组对照实测（同一脚本内 nowait postinstall / nowait /
    nowait postinstall skipifsilent）：前两条在 /VERYSILENT 下都执行了，
    日志 `-- Run entry -- / Run as: Original user`；带 skipifsilent 的那条
    整条被跳过。现场「升级完程序不见了、要手动双击图标」就是这么来的。
    """
    iss = LAUNCHER.parent.parent / "installer" / "kefu.iss"
    entries = [ln for ln in iss.read_text(encoding="utf-8").splitlines()
               if ln.startswith('Filename: "{app}\\{#MyAppExeName}"')]
    assert entries, "ISS 缺少 [Run] 启动条目"
    for line in entries:
        assert "skipifsilent" not in line, "静默安装不会启动程序"
        assert "postinstall" in line and "nowait" in line


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


# ---------------------------------------------------------------------------
# drain 线程健壮性：任何字节序列/异常都不允许杀死读取线程
# （drain 死 → 管道堵塞 → 后端事件循环阻塞 → 全局瘫痪）
# ---------------------------------------------------------------------------
def test_decode_line_never_raises(launcher):
    """utf-8/gbk/非法字节/空输入全部返回 str，绝不抛 UnicodeDecodeError。"""
    assert launcher._decode_line("中文日志".encode("utf-8")) == "中文日志"
    assert launcher._decode_line("中文日志".encode("gbk")) == "中文日志"
    # 非法字节（截断的 emoji 头 0xF0 0x9F、非法序列）→ replace 兜底
    out = launcher._decode_line(b"\xf0\x9f bad")
    assert isinstance(out, str)
    # emoji 本身合法 utf-8，应原样保留
    assert launcher._decode_line("😀".encode("utf-8")) == "😀"
    assert launcher._decode_line(b"") == ""
    # 混合：utf-8 前缀 + 尾部非法字节
    assert launcher._decode_line("ok ".encode("utf-8") + b"\xff\xfe").startswith("ok ")


def test_decode_line_prefers_utf8_over_gbk(launcher):
    """utf-8 可解时优先 utf-8（gbk 对部分 utf-8 字节也能蒙混解码）。"""
    text = "商机分析完成"
    assert launcher._decode_line(text.encode("utf-8")) == text


class _FakeStdout:
    """假 stdout：按预设序列返回字节行，耗尽后返回 b"" 模拟 EOF。"""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def readline(self) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _FlakyStdout:
    """前 fail_times 次 readline 抛 OSError，之后正常吐预设行直至 EOF。"""

    def __init__(self, fail_times: int, chunks):
        self._fails = fail_times
        self._chunks = list(chunks)

    def readline(self) -> bytes:
        if self._fails > 0:
            self._fails -= 1
            raise OSError("simulated pipe error")
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def _run_drain(launcher, readline_obj) -> list[str]:
    """在线程里跑 _drain_loop，等待退出后返回日志缓冲快照。"""
    launcher._state["log_lines"].clear()
    t = threading.Thread(target=launcher._drain_loop, args=(readline_obj.readline,),
                         daemon=True)
    t.start()
    t.join(timeout=5.0)
    assert not t.is_alive(), "drain 线程必须正常退出，不允许卡死或挂起"
    return list(launcher._state["log_lines"])


def test_drain_loop_survives_invalid_bytes(launcher):
    """混合合法/非法字节 + 结尾不换行的数据：全部入缓冲，线程正常退出。"""
    chunks = [
        b"INFO start\n",
        "微信消息😀".encode("utf-8") + b"\n",
        b"\xf0\x9f broken-emoji\n",          # 截断的 emoji 头（非法 utf-8/gbk）
        b"ok\xff\xfe mixed\n",
        b"no trailing newline",
        b"",
    ]
    logs = _run_drain(launcher, _FakeStdout(chunks))
    # 5 行数据 + 1 条 EOF 退出日志（空行被 _log 过滤）
    assert len(logs) == 6
    joined = "\n".join(logs)
    assert "INFO start" in joined
    assert "微信消息" in joined and "\U0001F600" in joined
    assert "broken-emoji" in joined, "非法字节行必须被替换/兜底解码，不能杀死线程"
    assert "no trailing newline" in joined
    assert any("输出流已结束" in line for line in logs), "EOF 须有退出日志"


def test_drain_loop_recovers_from_readline_errors(launcher):
    """readline 连续抛 OSError 后恢复：后续行仍能读到，线程最终正常退出。"""
    chunks = [
        b"before-failure\n",
        b"after-recovery\n",
        b"",
    ]
    logs = _run_drain(launcher, _FlakyStdout(fail_times=3, chunks=chunks))
    joined = "\n".join(logs)
    assert "before-failure" in joined
    assert "after-recovery" in joined, "异常恢复后必须继续读到后续行"
    assert len([ln for ln in logs if "自动恢复" in ln]) == 3


def test_drain_loop_logs_instead_of_dying_on_log_failure(launcher, monkeypatch):
    """_log 抛异常也不能让线程退出：记一条继续，直到 EOF。"""
    launcher._state["log_lines"].clear()
    calls: list[str] = []

    def flaky_log(line: str) -> None:
        calls.append(line)
        if len(calls) <= 2:
            raise RuntimeError("simulated log failure")

    monkeypatch.setattr(launcher, "_log", flaky_log)
    chunks = [b"line-one\n", b"line-two\n", b"line-three\n", b""]
    t = threading.Thread(target=launcher._drain_loop,
                         args=(_FakeStdout(chunks).readline,), daemon=True)
    t.start()
    t.join(timeout=5.0)
    assert not t.is_alive()
    # 3 行 + 2 次失败后的恢复日志 + 1 条 EOF 退出日志；失败吞掉的行允许丢失，
    # 但线程必须活到 EOF，后续行必须正常入账
    assert len(calls) == 5
    joined_calls = "\n".join(calls)
    assert "line-two" in joined_calls and "line-three" in joined_calls


def test_drain_loop_tolerates_log_failure_in_recovery_path(launcher, monkeypatch):
    """恢复路径里的 _log 也抛异常时线程仍继续（不因嵌套异常死亡）。"""
    launcher._state["log_lines"].clear()
    calls: list[int] = []

    def always_broken_log(line: str) -> None:
        calls.append(1)
        raise RuntimeError("log subsystem down")

    monkeypatch.setattr(launcher, "_log", always_broken_log)
    chunks = [b"a\n", b""]
    t = threading.Thread(target=launcher._drain_loop,
                         args=(_FakeStdout(chunks).readline,), daemon=True)
    t.start()
    t.join(timeout=5.0)
    assert not t.is_alive(), "即使 _log 全坏，线程也必须撑到 EOF 才退出"


def test_log_lines_deque_maxlen(launcher):
    """log_lines 用 maxlen 防内存无限增长：超限丢旧行、长度不超上限。"""
    maxlen = launcher._state["log_lines"].maxlen
    assert maxlen == 3000
    launcher._state["log_lines"].clear()
    for i in range(maxlen + 500):
        launcher._log(f"line-{i}")
    with launcher._state["log_lock"]:
        lines = list(launcher._state["log_lines"])
    assert len(lines) == maxlen
    assert "line-0" not in lines[-1], "最旧的行必须被丢弃"
    assert f"line-{maxlen + 499}" in lines[-1], "最新日志必须保留"


def test_start_backend_uses_binary_pipe(launcher):
    """Popen 必须二进制无缓冲读取（回归守门：禁止文本模式复活）。"""
    src = LAUNCHER.read_text(encoding="utf-8")
    start_block = src.split("def _start_backend")[1].split("def _status_url")[0]
    assert "bufsize=0" in start_block
    assert "universal_newlines" not in start_block
    assert "text=True" not in start_block
    # drain 主循环必须自带恢复逻辑且以 EOF 为唯一退出条件
    assert "_drain_loop(stdout.readline)" in src
    loop_block = src.split("def _drain_loop(")[1].split("def _start_backend")[0]
    assert "while True" in loop_block
    assert "return" in loop_block and "not raw" in loop_block
    # 解码兜底永不抛异常
    assert 'errors="replace"' in src
    # Edit 控件须显式放宽文本上限
    assert "EM_SETLIMITTEXT" in src
