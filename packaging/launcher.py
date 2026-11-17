# -*- coding: utf-8 -*-
"""P1 托盘启动器（编译成 launcher.pyc，由 start_kefu.bat 用内置 python 启动）。

职责：
  - 单实例互斥（CreateMutexW）：重复双击不启第二个后端
  - 用 bundle 内置 python 起后端子进程（run.pyc），CREATE_NEW_PROCESS_GROUP
  - 轮询端口就绪后 webbrowser.open 打开默认浏览器（自动打开一次）
  - 托盘图标（win32gui.Shell_NotifyIcon）：双击/菜单「打开操作界面」可再次打开
  - 菜单「检查更新」「退出」；退出发 CTRL_BREAK_EVENT 触发 run.py 优雅关闭
  - 注入全部环境变量：PATH、QT_ACCESSIBILITY、KEFU_APP_ROOT/DATA_DIR/ASSETS_DIR/
    VERSION_FILE、DLL 搜索目录

刻意不在本进程 import 业务代码（避免 UIA/COM 线程）；只做进程管理 + 托盘。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent
RUNTIME = BUNDLE / "runtime"
SITE = RUNTIME / "site-packages"
PYTHON = RUNTIME / "python" / "python.exe"
APP_CURRENT = BUNDLE / "app" / "current"
KEFU = APP_CURRENT / "kefu-system"
DATA = BUNDLE / "data"
CURRENT_JSON = BUNDLE / "current.json"
MUTEX_NAME = "Local\\WeChatKefuLauncher_SingleInstance"

try:
    import win32api
    import win32con
    import win32gui
    import win32process
except ImportError as exc:                        # pragma: no cover
    sys.stderr.write(f"[launcher] pywin32 不可用：{exc}\n")
    raise

_state = {"hwnd": None, "proc": None, "port": 8765, "opened": False, "nid": (1,)}


# ---------------------------------------------------------------------------
# 单实例
# ---------------------------------------------------------------------------
def _acquire_single_instance():
    import win32event
    import pywintypes
    try:
        # CreateMutex(bInitialOwner=True)：新建成功即持锁；若已存在则本进程
        # 不拥有它，WaitForSingleObject(mutex, 0) 返回 WAIT_OBJECT_0 之外的值。
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
        sys.stderr.write(f"[launcher] 创建版本入口失败：{err}\n")
    return ok


# ---------------------------------------------------------------------------
# 环境装配 + 起后端
# ---------------------------------------------------------------------------
def _read_port() -> int:
    try:
        s = os.environ.get("KEFU_PORT", "").strip()
        if s.isdigit() and 1 <= int(s) <= 65535:
            return int(s)
    except Exception:                              # noqa: BLE001
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
    return env


def _start_backend() -> subprocess.Popen:
    if not PYTHON.is_file():
        raise RuntimeError(f"内置 Python 缺失：{PYTHON}")
    run_pyc = KFU = KEFU / "run.pyc"
    if not run_pyc.is_file():
        # 开发态允许 run.py；发布态应是 pyc
        run_pyc = KEFU / "run.py"
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [str(PYTHON), str(run_pyc)], cwd=str(KEFU), env=_child_env(),
        creationflags=creationflags,
        stdin=subprocess.DEVNULL,
    )
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
            return False                           # 后端提前退出
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
MENU_UPDATE = 1002
MENU_EXIT = 1003
IDI_DEFAULT = win32con.IDI_APPLICATION


def _create_window():
    wc = win32gui.WNDCLASS()
    wc.lpfnWndProc = _wnd_proc
    wc.hInstance = win32api.GetModuleHandle(None)
    wc.lpszClassName = "WeChatKefuTray"
    wc.hIcon = win32gui.LoadIcon(0, IDI_DEFAULT)
    try:
        win32gui.RegisterClass(wc)
    except win32gui.error:                         # 已注册
        pass
    hwnd = win32gui.CreateWindow(
        wc.lpszClassName, "微信客服", 0, 0, 0, 0, 0, 0, 0,
        wc.hInstance, None)
    return hwnd


def _add_tray(hwnd):
    flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
    nid = (hwnd, _state["nid"][0], flags, WM_APP_TRAY,
           win32gui.LoadIcon(0, IDI_DEFAULT), "微信客服系统")
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
        subprocess.Popen([str(PYTHON), str(updater)], cwd=str(BUNDLE),
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
        elif cmd == MENU_UPDATE:
            _do_update()
        elif cmd == MENU_EXIT:
            _stop_backend()
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
        sys.stderr.write("[launcher] 已在运行，退出本次启动。\n")
        return

    if not _ensure_current():
        sys.stderr.write("[launcher] 版本层缺失或 current.json 无效，已停止启动。\n")
        return

    _state["port"] = _read_port()
    try:
        _state["proc"] = _start_backend()
    except Exception as exc:                       # noqa: BLE001
        sys.stderr.write(f"[launcher] 后端启动失败：{exc}\n")
        return

    # 起托盘窗口（即使浏览器没就绪也能手动打开）
    hwnd = _create_window()
    _state["hwnd"] = hwnd
    _add_tray(hwnd)

    # 等端口就绪后自动打开一次浏览器
    if _port_ready(_state["port"]):
        _open_ui()
    else:
        sys.stderr.write("[launcher] 后端未能在 30s 内就绪，请从托盘菜单手动打开。\n")

    # 消息循环（托盘交互）
    win32gui.PumpMessages()


if __name__ == "__main__":
    main()
