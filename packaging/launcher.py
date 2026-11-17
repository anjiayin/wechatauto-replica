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
  - 注入全部环境变量：PATH、QT_ACCESSIBILITY、KEFU_APP_ROOT/DATA_DIR/ASSETS_DIR/
    VERSION_FILE、KEFU_DELIVERY_SEED、DLL 搜索目录

刻意不在本进程 import 业务代码（避免 UIA/COM 线程）；只做进程管理 + 托盘 + 日志。
"""
from __future__ import annotations

import json
import os
import signal
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
ICON_FILE = BUNDLE / "app.ico"
MUTEX_NAME = "Local\\WeChatKefuLauncher_SingleInstance"

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
    "hwnd": None, "proc": None, "port": 8765, "opened": False, "nid": (1,),
    "log_hwnd": None, "log_edit": None, "log_lines": deque(maxlen=2000),
    "log_lock": threading.Lock(), "log_timer": None,
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
def _read_port() -> int:
    try:
        s = os.environ.get("KEFU_PORT", "").strip()
        if s.isdigit() and 1 <= int(s) <= 65535:
            return int(s)
    except Exception:                            # noqa: BLE001
        pass
    return 8765


def _child_env() -> dict:
    env = os.environ.copy()
    env["PATH"] = str(RUNTIME) + os.pathsep + env.get("PATH", "")
    env["QT_ACCESSIBILITY"] = "1"
    env["KEFU_APP_ROOT"] = str(KEFU)
    env["KEFU_DATA_DIR"] = str(DATA)
    env["KEFU_ASSETS_DIR"] = str(RUNTIME)
    env["KEFU_VERSION_FILE"] = str(KEFU / "version.txt")
    env.setdefault("KEFU_LICENSE_MODE", "enforce")
    # 交付 seed（鉴权生产地址 + 预置商机类型）；开发态无此文件则不生效
    if SEED_JSON.is_file():
        env["KEFU_DELIVERY_SEED"] = str(SEED_JSON)
    return env


# ---------------------------------------------------------------------------
# 日志捕获 + GUI 日志窗口
# ---------------------------------------------------------------------------
def _log(line: str) -> None:
    """把一行文本写入日志缓冲（供 GUI 日志窗口显示）。"""
    line = line.rstrip("\r\n")
    if not line:
        return
    stamp = time.strftime("%H:%M:%S")
    with _state["log_lock"]:
        _state["log_lines"].append(f"[{stamp}] {line}")


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
    try:
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
        bufsize=1, universal_newlines=True,
    )
    # 后台线程逐行读后端输出 → 日志缓冲
    def _drain():
        try:
            for line in proc.stdout:
                _log(line)
        except Exception:                        # noqa: BLE001
            pass
    t = threading.Thread(target=_drain, daemon=True)
    t.start()
    return proc


def _port_ready(port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/api/status"
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
    url = f"http://127.0.0.1:{_state['port']}/"
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


def main() -> None:
    mutex = _acquire_single_instance()
    if mutex is None:
        _log("已在运行，退出本次启动。")
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
