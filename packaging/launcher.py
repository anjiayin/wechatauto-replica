# -*- coding: utf-8 -*-
"""P1 托盘启动器（编译成 launcher.pyc，由 start_kefu.bat 用内置 pythonw 启动）。

职责：
  - 单实例互斥（CreateMutexW）：重复双击不启第二个后端
  - 用 bundle 内置 pythonw 起后端子进程（run.pyc），CREATE_NEW_PROCESS_GROUP
  - 轮询端口就绪后 webbrowser.open 打开默认浏览器（自动打开一次）
  - 托盘图标（win32gui.Shell_NotifyIcon，用 bundle 根 app.ico）：双击/菜单
    「打开操作界面」可再次打开；菜单「查看/隐藏日志」「检查更新」「退出」
  - 日志窗口：自建 GUI 窗口（默认隐藏），捕获后端 stdout/stderr；点关闭按钮
    只隐藏不退进程；只有托盘「退出」才真正停后端并退出
  - 注入全部环境变量：PATH、QT_ACCESSIBILITY、KEFU_APP_ROOT/KEFU_BUNDLE_ROOT/
    KEFU_DATA_DIR/ASSETS_DIR/VERSION_FILE、KEFU_DELIVERY_SEED、DLL 搜索目录
  - 升级接力：轮询 data/update_handoff.json，增量升级装完自动原地重启后端，
    全量升级确认后弹托盘气泡「已升级到 x.y.z」（见 _start_handoff_watcher）
  - 日志除 GUI 窗口外同步落 data/launcher.log（pythonw 没有控制台，这是事后
    唯一取证入口）

刻意不在本进程 import 业务代码（避免 UIA/COM 线程）；只做进程管理 + 托盘 + 日志。
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from collections import deque
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent
RUNTIME = BUNDLE / "runtime"
SITE = RUNTIME / "site-packages"
PYTHON = RUNTIME / "python" / "pythonw.exe"          # 无控制台：日志走自建 GUI 窗口
PYTHON_FALLBACK = RUNTIME / "python" / "python.exe"
APP_CURRENT = BUNDLE / "app" / "current"
KEFU = APP_CURRENT / "kefu-system"
DATA = BUNDLE / "data"
CURRENT_JSON = BUNDLE / "current.json"
SEED_JSON = BUNDLE / "delivery_seed.json"
UPDATE_CONFIG = BUNDLE / "update_config.json"             # 自动更新地址/渠道（发布配置）
ICON_FILE = BUNDLE / "app.ico"
MUTEX_NAME = "Local\\WeChatKefuLauncher_SingleInstance"
DEFAULT_PORT = 8765                                          # 与 run.py 的 DEFAULT_PORT 一致
SETTING_KEY_PORT = "run.port"                                # 与 run.py/router_settings.py 一致
DB_NAME = "kefu.sqlite3"                                     # 与 app.config.DB_PATH 一致
# 升级接力文件（写入方 app/update_core.write_handoff，schema 改动必须两边同步）：
# 本进程刻意不 import 业务模块，故这里独立实现读取，不复用 update_core。
HANDOFF_FILE_NAME = "update_handoff.json"
HANDOFF_TTL_SEC = 15 * 60          # 与 app/update_core.HANDOFF_TTL_SEC 一致
HANDOFF_POLL_SEC = 2.0
KIND_RESTART = "restart"
KIND_NOTIFY = "notify"

def _fatal(msg: str) -> None:
    """pythonw 无控制台：致命错误写文件，便于排查。"""
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with open(DATA / "launcher-error.log", "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg + "\n")
    except Exception:                            # noqa: BLE001
        pass


try:
    import win32api
    import win32con
    import win32gui
    import win32process
except ImportError as exc:                        # pragma: no cover
    _fatal(f"[launcher] pywin32 不可用：{exc}")
    raise

_state = {
    "hwnd": None, "proc": None, "port": DEFAULT_PORT, "opened": False, "nid": (1,),
    "log_hwnd": None, "log_edit": None, "log_lines": deque(maxlen=3000),
    "log_lock": threading.Lock(), "log_timer": None,
    # 只执行本次启动之后写入的接力请求：更早的请求属于上一个启动器进程，
    # 照它重启会杀掉刚起来的后端（还可能打断正在进行的下载/安装）。
    "started_at": int(time.time()), "exiting": False,
}


# ---------------------------------------------------------------------------
# 单实例
# ---------------------------------------------------------------------------
def _acquire_single_instance():
    import win32event
    import pywintypes
    try:
        mutex = win32event.CreateMutex(None, True, MUTEX_NAME)
        if win32event.WaitForSingleObject(mutex, 0) != win32event.WAIT_OBJECT_0:
            return None
    except (pywintypes.error, AttributeError):
        return None
    return mutex


# ---------------------------------------------------------------------------
# 版本层入口：安装包刻意不含 app/current junction，首次启动按 current.json 创建
# ---------------------------------------------------------------------------
def _rebuild_junction(link: Path, target: Path) -> tuple[bool, str]:
    """本进程不能 import 业务模块（此时 junction 可能还不存在），故独立实现。"""
    ps = (
        f"$i = Get-Item -LiteralPath '{link}' -Force -ErrorAction SilentlyContinue; "
        f"if ($i) {{ $i.Delete() }}; "
        f"New-Item -ItemType Junction -Path '{link}' -Target '{target}' "
        f"-ErrorAction Stop | Out-Null"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=30)
    except Exception as exc:                      # noqa: BLE001
        return False, str(exc)
    return r.returncode == 0, r.stderr.strip()


def _ensure_current() -> bool:
    """确保 app/current 指向 current.json 声明的版本（安装器不随包带 junction）。"""
    try:
        version = str(json.loads(CURRENT_JSON.read_text(encoding="utf-8"))["version"]).strip()
    except Exception:                            # noqa: BLE001
        return False
    if not version or any(c in version for c in ('"', "'", "\\", "/", ":")):
        return False
    target = BUNDLE / "app" / version
    run_py = target / "kefu-system" / "run.pyc"
    if not run_py.is_file() and not (target / "kefu-system" / "run.py").is_file():
        return False
    link = BUNDLE / "app" / "current"
    try:
        if link.exists() and link.resolve() == target.resolve():
            return True
    except OSError:                              # noqa: BLE001
        pass
    ok, err = _rebuild_junction(link, target)
    if not ok:
        _log(f"[launcher] 创建版本入口失败：{err}")
    return ok


# ---------------------------------------------------------------------------
# 环境装配 + 起后端
# ---------------------------------------------------------------------------
def _parse_port(value: object) -> int | None:
    """把端口取值规整为 1~65535 的整数；非法返回 None（fail-safe）。

    Args:
        value: 环境变量字符串或配置库里的原始值。

    Returns:
        合法端口号，或 None（空值/非整数/越界/类型异常）。
    """
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _read_saved_port() -> int | None:
    """只读查询配置库 ``settings.run.port``；任何异常都回退 None。

    配置库路径与后端 ``app.config.DB_PATH`` 保持一致：优先取 KEFU_DATA_DIR，
    否则用 bundle 的 data 目录（也是 ``_child_env()`` 注入给后端的目录）。
    使用 URI ``mode=ro`` 连接，绝不创建/写入 DB，也不与后端抢写锁。
    """
    raw_dir = os.environ.get("KEFU_DATA_DIR", "").strip()
    data_dir = Path(raw_dir) if raw_dir else DATA
    db_path = data_dir / DB_NAME
    if not db_path.is_file():
        return None
    conn: sqlite3.Connection | None = None
    try:
        # as_uri() 会转义路径里的空格/# 等字符；ro 模式下缺表/缺库均不报致命错。
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro",
                               uri=True, timeout=1.0)
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (SETTING_KEY_PORT,)
        ).fetchone()
        if not row:
            return None
        return _parse_port(row[0])
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:                  # noqa: BLE001
                pass


def _read_port() -> int:
    """解析启动器端口：环境变量 KEFU_PORT > 配置库 run.port > 默认 8765。

    与后端 ``run.py::_resolve_address_detail`` 的优先级一致（启动器不传
    ``--port``，且后端继承的环境变量未被改写，因此不存在第四个来源）。
    刻意**不**把配置库端口写进 ``KEFU_PORT`` 环境变量：那会抬高配置值的
    优先级，破坏后端既有的「命令行 > 环境变量 > 配置库 > 默认」语义。
    """
    env_port = _parse_port(os.environ.get("KEFU_PORT", ""))
    if env_port is not None:
        return env_port
    saved_port = _read_saved_port()
    if saved_port is not None:
        return saved_port
    return DEFAULT_PORT


def _child_env() -> dict:
    env = os.environ.copy()
    env["PATH"] = str(RUNTIME) + os.pathsep + env.get("PATH", "")
    env["QT_ACCESSIBILITY"] = "1"
    env["KEFU_APP_ROOT"] = str(KEFU)
    # 权威安装根：后端 update_service.bundle_root() 与托盘 updater.py 都优先
    # 用它。不能只靠 APP_ROOT 反推——Windows 上 app/current 是 junction，
    # Path.resolve() 会解析成 app/<版本>，导致安装版被误判为开发态而拒绝更新。
    env["KEFU_BUNDLE_ROOT"] = str(BUNDLE)
    env["KEFU_DATA_DIR"] = str(DATA)
    env["KEFU_ASSETS_DIR"] = str(RUNTIME)
    env["KEFU_VERSION_FILE"] = str(KEFU / "version.txt")
    env.setdefault("KEFU_LICENSE_MODE", "enforce")
    # 交付 seed（鉴权生产地址 + 预置商机类型）；开发态无此文件则不生效
    if SEED_JSON.is_file():
        env["KEFU_DELIVERY_SEED"] = str(SEED_JSON)
    # 自动更新：bundle 根的 update_config.json 提供 manifest 地址/渠道，
    # 注入给后端（update_service 模块 import 时读取）与托盘更新器。
    # 未配置或地址为空则保持 fail-safe（不检查更新）。
    if UPDATE_CONFIG.is_file():
        try:
            cfg = json.loads(UPDATE_CONFIG.read_text(encoding="utf-8"))
            url = str(cfg.get("manifest_url", "")).strip().rstrip("/")
            if url.startswith(("http://", "https://")):
                env["KEFU_UPDATE_MANIFEST_URL"] = url
            channel = str(cfg.get("channel", "")).strip()
            if channel:
                env["KEFU_UPDATE_CHANNEL"] = channel
        except Exception:                        # noqa: BLE001
            pass                                  # 配置损坏则保持 fail-safe，不阻断启动
    return env


# ---------------------------------------------------------------------------
# 日志捕获 + GUI 日志窗口
# ---------------------------------------------------------------------------
# 落盘文件名与大小上限：启动器是 pythonw（没有控制台），日志窗口又是默认隐藏的，
# 出问题（升级没起来、后端反复重启失败）时磁盘上这份是唯一的事后取证。
LOG_FILE_NAME = "launcher.log"
LOG_MAX_BYTES = 1024 * 1024          # 超限轮转，只留一份历史 launcher.log.1


def _append_log_file(line: str) -> None:
    """把一行日志追加到 data/launcher.log；任何 IO 异常都吞掉。

    绝不允许影响 GUI 缓冲（日志窗口是排障第一入口），也不允许让 drain 线程因为
    写盘失败而死——写盘失败的最坏后果只是「事后没日志可查」。
    """
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        path = DATA / LOG_FILE_NAME
        try:
            if path.stat().st_size > LOG_MAX_BYTES:
                os.replace(path, DATA / (LOG_FILE_NAME + ".1"))
        except OSError:
            pass                      # 新建/轮转失败继续按追加走
        with open(path, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except Exception:                 # noqa: BLE001
        pass


def _log(line: str) -> None:
    """把一行文本写入日志缓冲（供 GUI 日志窗口显示）并落 data/launcher.log。"""
    line = line.rstrip("\r\n")
    if not line:
        return
    _append_log_file(line)
    stamp = time.strftime("%H:%M:%S")
    with _state["log_lock"]:
        _state["log_lines"].append(f"[{stamp}] {line}")


# 日志 Edit 控件文本上限：32KB 默认上限会静默吞掉新日志，显式放宽到 1MB
EDIT_TEXT_LIMIT = 0x100000
# 追加前文本长度警戒线：接近上限先清空再追加，保证最新日志永远可见
EDIT_TRIM_THRESHOLD = 900 * 1024


def _pump_log() -> None:
    """定时把缓冲里的新日志刷到 Edit 控件（跨线程用 SendMessage 安全）。"""
    edit = _state["log_edit"]
    if not edit:
        return
    with _state["log_lock"]:
        lines = list(_state["log_lines"])
    if not lines:
        return
    text = "\r\n".join(lines) + "\r\n"
    # 单批文本本身超警戒线时丢弃最旧的行，避免追加被控件上限静默截断
    if len(text) > EDIT_TRIM_THRESHOLD:
        while lines and len("\r\n".join(lines)) + 2 > EDIT_TRIM_THRESHOLD:
            lines.pop(0)
        text = "\r\n".join(lines) + "\r\n"
    try:
        # 控件已有文本接近上限时先清空，防止 EM_REPLACESEL 从尾部静默截断
        if win32gui.GetWindowTextLength(edit) > EDIT_TRIM_THRESHOLD:
            win32gui.SendMessage(edit, win32con.EM_SETSEL, 0, -1)
            win32gui.SendMessage(edit, win32con.EM_REPLACESEL, 0, "")
        win32gui.SendMessage(edit, win32con.EM_SETSEL, -1, -1)
        win32gui.SendMessage(edit, win32con.EM_REPLACESEL, 0, text)
    except Exception:                            # noqa: BLE001
        pass


def _start_log_timer() -> None:
    def _loop():
        while True:
            time.sleep(1.0)
            if _state["log_hwnd"] and win32gui.IsWindow(_state["log_hwnd"]):
                win32gui.PostMessage(_state["log_hwnd"], win32con.WM_APP + 30, 0, 0)
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    _state["log_timer"] = t


WM_FLUSH_LOG = win32con.WM_APP + 30


def _log_wnd_proc(hwnd, msg, wparam, lparam):
    if msg == WM_FLUSH_LOG:
        _pump_log()
        return 0
    if msg == win32con.WM_SIZE:
        # 让 Edit 控件铺满客户区
        edit = _state["log_edit"]
        if edit:
            w = win32api.LOWORD(lparam)
            h = win32api.HIWORD(lparam)
            win32gui.MoveWindow(edit, 0, 0, w, h, True)
        return 0
    if msg == win32con.WM_CLOSE:
        # 关日志窗口 = 隐藏，不退进程
        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        return 0
    if msg == win32con.WM_DESTROY:
        _state["log_hwnd"] = None
        _state["log_edit"] = None
        return 0
    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)


def _create_log_window():
    wc = win32gui.WNDCLASS()
    wc.lpfnWndProc = _log_wnd_proc
    wc.hInstance = win32api.GetModuleHandle(None)
    wc.lpszClassName = "WeChatKefuLog"
    wc.hIcon = _load_icon(32)
    wc.hbrBackground = win32con.COLOR_WINDOW + 1
    try:
        win32gui.RegisterClass(wc)
    except win32gui.error:                         # 已注册
        pass
    hwnd = win32gui.CreateWindow(
        wc.lpszClassName, "微信客服 - 运行日志",
        win32con.WS_OVERLAPPEDWINDOW,
        120, 120, 900, 560, 0, 0, wc.hInstance, None)
    # Edit 子控件：多行、只读、自动滚动
    edit = win32gui.CreateWindow(
        "Edit", "",
        win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.WS_VSCROLL |
        win32con.ES_MULTILINE | win32con.ES_READONLY | win32con.ES_AUTOVSCROLL,
        0, 0, 900, 560, hwnd, 0, wc.hInstance, None)
    try:
        hfont = win32gui.GetStockObject(win32con.SYSTEM_FONT)
        win32gui.SendMessage(edit, win32con.WM_SETFONT, hfont, 0)
    except Exception:                            # noqa: BLE001
        pass
    # 放宽 Edit 文本上限（默认 32KB），配合 _pump_log 的裁剪逻辑，
    # 保证日志窗口不因默认上限静默失效、永远能滚动看到最新日志
    try:
        win32gui.SendMessage(edit, win32con.EM_SETLIMITTEXT, EDIT_TEXT_LIMIT, 0)
    except Exception:                            # noqa: BLE001
        pass
    _state["log_hwnd"] = hwnd
    _state["log_edit"] = edit
    win32gui.ShowWindow(hwnd, win32con.SW_HIDE)   # 默认隐藏
    return hwnd


def _toggle_log() -> None:
    hwnd = _state["log_hwnd"]
    if not hwnd or not win32gui.IsWindow(hwnd):
        return
    if win32gui.IsWindowVisible(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
    else:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        win32gui.SetForegroundWindow(hwnd)
        _pump_log()


# ---------------------------------------------------------------------------
# 图标
# ---------------------------------------------------------------------------
def _load_icon(size: int):
    """从 bundle 根 app.ico 加载图标；缺失时回退系统默认图标。"""
    if ICON_FILE.is_file():
        try:
            return win32gui.LoadImage(0, str(ICON_FILE), win32con.IMAGE_ICON,
                                      size, size, win32con.LR_LOADFROMFILE)
        except Exception:                        # noqa: BLE001
            pass
    return win32gui.LoadIcon(0, win32con.IDI_APPLICATION)


# ---------------------------------------------------------------------------
# 起后端（stdout/stderr 捕获到日志窗口）
# ---------------------------------------------------------------------------
def _decode_line(raw: bytes) -> str:
    """把一行原始字节解码为文本，任何字节序列都不抛异常。

    解码策略：先试 utf-8（后端正常输出），失败再试 gbk（历史遗留/控制台
    本地化输出），仍失败用 utf-8 + errors="replace" 兜底，保证 drain 线程
    不会因 emoji/特殊字符等非法字节而死（那是导致后端全局瘫痪的根因）。

    Args:
        raw: 管道里读到的一行原始字节（可含/不含换行符）。

    Returns:
        解码后的文本（非法字节替换为 U+FFFD），空输入返回空串。
    """
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _drain_loop(readline) -> None:
    """日志读取线程主循环：绝不因异常退出，仅在后端 EOF 时正常结束。

    drain 线程死亡 → 无人排空 stdout 管道 → 管道缓冲写满 → 后端所有写
    stdout 的线程（含 uvicorn 事件循环）永久阻塞 → 后端全局瘫痪。因此
    readline 系统错误、解码异常、_log 异常都只记录后继续循环，绝不允许
    线程退出；EOF（readline 返回 b""，即后端进程退出）才正常退出。

    Args:
        readline: 无参可调用，返回一行字节（b"" 表示 EOF），可能抛异常。
    """
    while True:
        try:
            raw = readline()
        except Exception as exc:                 # noqa: BLE001
            # 短暂休眠避免流持续报错时空转烧 CPU
            time.sleep(0.05)
            try:
                _log(f"[launcher] 日志读取异常，线程自动恢复：{exc!r}")
            except Exception:                    # noqa: BLE001
                pass
            continue
        if not raw:
            try:
                _log("[launcher] 后端输出流已结束，日志读取线程退出。")
            except Exception:                    # noqa: BLE001
                pass
            return
        try:
            _log(_decode_line(raw))
        except Exception as exc:                 # noqa: BLE001
            try:
                _log(f"[launcher] 日志行处理异常，线程继续：{exc!r}")
            except Exception:                    # noqa: BLE001
                pass


def _start_backend() -> subprocess.Popen:
    py = PYTHON if PYTHON.is_file() else PYTHON_FALLBACK
    if not py.is_file():
        raise RuntimeError(f"内置 Python 缺失：{PYTHON}")
    run_pyc = KEFU / "run.pyc"
    if not run_pyc.is_file():
        run_pyc = KEFU / "run.py"                # 开发态允许
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    if py == PYTHON:                             # pythonw 无控制台，不额外建窗口
        creationflags |= subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        [str(py), str(run_pyc)], cwd=str(KEFU), env=_child_env(),
        creationflags=creationflags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=0,   # 二进制无缓冲：解码交给 _decode_line，任何字节都不致命
    )
    # 后台线程逐行读后端输出（二进制）→ 解码 → 日志缓冲。
    # 旧实现用文本模式迭代 proc.stdout，遇到当前 locale 无法解码的字节
    # （如 cp936 遇到 emoji）会抛 UnicodeDecodeError 静默杀死线程，进而
    # 管道堵塞拖垮整个后端；这里改为二进制读取 + 永不退出的恢复循环。
    def _drain():
        stdout = proc.stdout
        if stdout is None:                       # 理论不可达（stdout=PIPE）
            return
        _drain_loop(stdout.readline)
    t = threading.Thread(target=_drain, daemon=True)
    t.start()
    return proc


def _status_url(port: int) -> str:
    """后端就绪探测 URL（集中拼接，便于测试断言端口来源）。"""
    return f"http://127.0.0.1:{port}/api/status"


def _ui_url(port: int) -> str:
    """操作界面 URL（集中拼接，确保打开的是解析出的真实端口）。"""
    return f"http://127.0.0.1:{port}/"


def _port_ready(port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    url = _status_url(port)
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:                          # noqa: BLE001
            pass
        if _state["proc"] and _state["proc"].poll() is not None:
            return False
        time.sleep(0.4)
    return False


def _open_ui():
    url = _ui_url(_state["port"])
    webbrowser.open(url)
    _state["opened"] = True


# ---------------------------------------------------------------------------
# 托盘
# ---------------------------------------------------------------------------
WM_APP_TRAY = win32con.WM_USER + 20
MENU_OPEN = 1001
MENU_LOG = 1002
MENU_UPDATE = 1003
MENU_EXIT = 1004


def _create_window():
    wc = win32gui.WNDCLASS()
    wc.lpfnWndProc = _wnd_proc
    wc.hInstance = win32api.GetModuleHandle(None)
    wc.lpszClassName = "WeChatKefuTray"
    wc.hIcon = _load_icon(32)
    try:
        win32gui.RegisterClass(wc)
    except win32gui.error:
        pass
    hwnd = win32gui.CreateWindow(
        wc.lpszClassName, "微信客服", 0, 0, 0, 0, 0, 0, 0,
        wc.hInstance, None)
    return hwnd


def _add_tray(hwnd):
    flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
    nid = (hwnd, _state["nid"][0], flags, WM_APP_TRAY,
           _load_icon(16), "微信客服系统")
    win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)


def _remove_tray(hwnd):
    nid = (hwnd, _state["nid"][0], 0, 0, 0, "")
    try:
        win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, nid)
    except Exception:                              # noqa: BLE001
        pass


def _show_menu(hwnd):
    menu = win32gui.CreatePopupMenu()
    win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_OPEN, "打开操作界面")
    win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_LOG, "查看/隐藏日志")
    win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_UPDATE, "检查更新…")
    win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
    win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_EXIT, "退出")
    pos = win32gui.GetCursorPos()
    win32gui.SetForegroundWindow(hwnd)
    win32gui.TrackPopupMenu(menu, win32con.TPM_LEFTALIGN | win32con.TPM_RIGHTBUTTON,
                            pos[0], pos[1], 0, hwnd, None)
    win32gui.DestroyMenu(menu)


def _stop_backend():
    proc = _state["proc"]
    if proc and proc.poll() is None:
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except Exception:                          # noqa: BLE001
            pass
        try:
            proc.wait(timeout=25)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def _do_update():
    """拉起独立更新器脚本；更新器会提示关杀软、下载、切换后退出本启动器。"""
    updater = BUNDLE / "updater.pyc"
    if not updater.is_file():
        updater = BUNDLE / "updater.py"
    if updater.is_file():
        py = PYTHON if PYTHON.is_file() else PYTHON_FALLBACK
        subprocess.Popen([str(py), str(updater)], cwd=str(BUNDLE),
                         env=_child_env(), creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)


# ---------------------------------------------------------------------------
# 升级接力：增量装完自动重启后端 + 升级完成托盘气泡提示
# ---------------------------------------------------------------------------
# 写入方是 app/update_core.write_handoff（后端 apply / 托盘 updater 调用）。
# 为什么这件事只能由启动器做：「重启后端」= 停掉子进程再重新 spawn，而后端自己
# 做不到（它一死，托盘只会看到进程退出，不会重新拉起——拉起是 main 的一次性逻辑）；
# 用 kill + 外部计划任务更不行，1.3.6 现场已证明硬杀进程树的代价。
# 老版启动器不认识这个文件（本能力随 launcher.pyc 一起发布），届时只是不自动
# 重启，升级本身仍然成功 —— 请求文件会在 TTL 后被忽略并删除，不会累积。
def _handoff_path() -> Path:
    return DATA / HANDOFF_FILE_NAME


def _take_handoff(now_ts: int | None = None) -> dict | None:
    """读走一条升级接力请求（读到即删，一次请求只执行一次），不可执行返回 None。

    两道守卫缺一不可：
      - at < 本次启动时刻：属于上一个启动器进程的请求，照做会重启刚起来的后端；
      - 超 TTL：apply 半途失败留下的陈旧请求，照做会杀掉此刻正在下载全量包的进程。
    删除失败不影响正确性：at 守卫会让同一请求第二次被忽略。
    """
    path = _handoff_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None                              # 没有请求 / 读到半个 JSON
    try:
        path.unlink()
    except OSError:
        pass
    if not isinstance(raw, dict):
        return None
    at = raw.get("at")
    if not isinstance(at, int) or isinstance(at, bool):
        return None
    now = int(time.time() if now_ts is None else now_ts)
    if at < _state["started_at"] or now - at > HANDOFF_TTL_SEC:
        _log(f"[launcher] 忽略陈旧升级接力请求（版本 {raw.get('version')}）")
        return None
    return raw


def _notify(title: str, text: str) -> bool:
    """托盘气泡提示（Vista 起的 Shell_NotifyIcon NIF_INFO）。

    pywin32 的 Shell_NotifyIcon 只吃 6 元组（NIM_ADD 那套），带不了 szInfo/
    szInfoTitle/dwInfoFlags，所以气泡必须用 ctypes 手搓 NOTIFYICONDATAW。
    提示看不见不能影响服务本身：任何失败都返回 False，由调用方落日志。
    """
    hwnd = _state["hwnd"]
    if not hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class _NID(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uTimeout", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16),
                ("hBalloonIcon", wintypes.HICON),
            ]

        nid = _NID()
        nid.cbSize = ctypes.sizeof(_NID)         # 结构体大小决定 shell 版本判定
        nid.hWnd = hwnd
        nid.uID = _state["nid"][0]
        nid.uFlags = 0x00000010                  # NIF_INFO：只改提示，不动图标
        nid.szInfo = text[:254]
        nid.szInfoTitle = title[:62]
        nid.dwInfoFlags = 0x00000001             # NIIF_INFO（蓝色 i 图标）
        return bool(ctypes.windll.shell32.Shell_NotifyIconW(0x00000001,
                                                           ctypes.byref(nid)))
    except Exception as exc:                     # noqa: BLE001
        _log(f"[launcher] 托盘气泡提示失败：{exc}")
        return False


def _notify_or_log(title: str, text: str) -> None:
    """气泡弹不出来（无窗口站/远程桌面/shell 拒绝）也要在日志里留一行。"""
    if not _notify(title, text):
        _log(f"[launcher] {title}：{text}")


def _restart_backend_for_update(version: str = "") -> bool:
    """原地重启后端，让刚切好的新版本层真正跑起来（不必人工退出再双击图标）。

    只在守护线程里调用：_stop_backend 最坏等 25s+5s，放消息循环里托盘会假死。
    """
    _log(f"增量升级已落地（版本 {version or '未知'}），正在重启后端…")
    _stop_backend()
    _state["proc"] = None
    if _state["exiting"]:                        # 用户在这 30 秒里点了「退出」
        _log("收到退出请求，不再重启后端。")
        return False
    _state["port"] = _read_port()
    try:
        _state["proc"] = _start_backend()
    except Exception as exc:                     # noqa: BLE001
        _notify_or_log("微信客服更新未完成",
                       f"新版本 {version} 已安装，但后端重启失败：{exc}\n"
                       "请右键托盘图标「退出」后重新打开程序。")
        return False
    if not _port_ready(_state["port"], timeout=60.0):
        _notify_or_log("微信客服更新未完成",
                       f"新版本 {version} 已安装，但后端未能在 60s 内就绪。\n"
                       "请右键托盘图标「查看/隐藏日志」排查，或退出后重新打开程序。")
        return False
    _open_ui()
    _notify_or_log("微信客服已更新",
                   f"已升级到 {version}，操作界面已重新打开。" if version
                   else "后端已重启，操作界面已重新打开。")
    return True


def _handle_handoff(info: dict) -> bool:
    kind = str(info.get("kind") or "")
    version = str(info.get("version") or "").strip()
    if kind == KIND_RESTART:
        return _restart_backend_for_update(version)
    if kind == KIND_NOTIFY:
        # 全量升级由 Inno 自己重启程序，装完没有任何提示；这里补一条气泡。
        _notify_or_log("微信客服已更新",
                       f"已升级到 {version}，可正常使用。" if version else "升级完成。")
        return True
    _log(f"[launcher] 忽略未知类型的升级接力请求：{kind}")
    return False


def _start_handoff_watcher():
    """守护线程轮询升级接力文件；任何异常都只记日志，线程绝不退出。

    线程一死就再没人自动重启后端、再没人提示升级完成，而且用户完全看不出来
    ——与 _drain_loop 同一套「宁可刷错误日志也不能死」的取舍。
    """
    def _loop():
        while True:
            try:
                time.sleep(HANDOFF_POLL_SEC)
                info = _take_handoff()
                if info is not None:
                    # 本线程是请求的唯一消费者，且重启是阻塞式的：不存在两个请求
                    # 并行执行，因此无需加锁。
                    _handle_handoff(info)
            except Exception as exc:             # noqa: BLE001
                try:
                    _log(f"[launcher] 升级接力轮询异常（自动恢复）：{exc}")
                except Exception:                # noqa: BLE001
                    pass

    t = threading.Thread(target=_loop, daemon=True, name="kefu-handoff-watch")
    t.start()
    return t


def _wnd_proc(hwnd, msg, wparam, lparam):
    if msg == WM_APP_TRAY:
        event = lparam & 0xFFFF
        if event == win32con.WM_LBUTTONDBLCLK:
            _open_ui()
        elif event == win32con.WM_RBUTTONUP:
            _show_menu(hwnd)
        return 0
    if msg == win32con.WM_COMMAND:
        cmd = wparam & 0xFFFF
        if cmd == MENU_OPEN:
            _open_ui()
        elif cmd == MENU_LOG:
            _toggle_log()
        elif cmd == MENU_UPDATE:
            _do_update()
        elif cmd == MENU_EXIT:
            # 先立旗：升级接力线程可能正卡在 _stop_backend 的 30s 等待里，
            # 必须让它看见「用户要退出」，否则它会在我们退出后把后端又拉起来。
            _state["exiting"] = True
            _stop_backend()
            log_hwnd = _state["log_hwnd"]
            if log_hwnd and win32gui.IsWindow(log_hwnd):
                win32gui.DestroyWindow(log_hwnd)
            _remove_tray(hwnd)
            win32gui.PostQuitMessage(0)
        return 0
    if msg == win32con.WM_DESTROY:
        _remove_tray(hwnd)
        win32gui.PostQuitMessage(0)
        return 0
    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)


MUTEX_ALREADY_RUNNING_TITLE = "微信客服"
MUTEX_ALREADY_RUNNING_TEXT = (
    "微信客服已在运行，请查看系统托盘图标（屏幕右下角）。\n\n"
    "如需重启服务：请先从托盘图标右键菜单「退出」，再重新启动程序。"
)


def _show_already_running_message() -> None:
    """重复启动拦截时弹模态提示（pythonw 无控制台，这是用户唯一可见反馈）。

    此时日志窗口尚未创建，仅 _log 用户看不到，故必须用 MessageBox 兜底。
    弹框失败（如无窗口站）不能影响退出路径，异常吞掉即可。
    """
    try:
        win32gui.MessageBox(0, MUTEX_ALREADY_RUNNING_TEXT,
                            MUTEX_ALREADY_RUNNING_TITLE,
                            win32con.MB_OK | win32con.MB_ICONINFORMATION)
    except Exception:                              # noqa: BLE001
        pass


def main() -> None:
    mutex = _acquire_single_instance()
    if mutex is None:
        _log("已在运行，退出本次启动。")
        _show_already_running_message()
        return

    if not _ensure_current():
        _log("版本层缺失或 current.json 无效，已停止启动。")
        return

    _state["port"] = _read_port()
    # 日志窗口先建（默认隐藏），后端输出随即有归属
    _create_log_window()
    _start_log_timer()
    _log("正在启动微信客服后端…")

    try:
        _state["proc"] = _start_backend()
    except Exception as exc:                       # noqa: BLE001
        _log(f"后端启动失败：{exc}")
        return

    hwnd = _create_window()
    _state["hwnd"] = hwnd
    _add_tray(hwnd)
    # 托盘建好才能弹气泡（NIF_INFO 挂在已有图标上），故在 _add_tray 之后启动
    _start_handoff_watcher()

    if _port_ready(_state["port"]):
        _log("后端就绪，正在打开操作界面。")
        _open_ui()
    else:
        _log("后端未能在 30s 内就绪，请从托盘菜单手动打开（可查看日志排查）。")

    win32gui.PumpMessages()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:                      # noqa: BLE001  pythonw 无控制台
        import traceback
        _fatal("[launcher] 未捕获异常：\n" + traceback.format_exc())
