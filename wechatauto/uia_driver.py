# -*- coding: utf-8 -*-
"""wechatauto.uia_driver —— 基于 UI Automation 的微信 4.x 混合驱动引擎（可选路径）

背景
----
微信 4.1.12+ 的聊天区域使用自绘渲染，冷启动时对 UIAutomation 只暴露
``Qt51514QWindowIcon`` 空壳（2 个子节点），看不到任何 ``mmui::`` 控件——
这是旧版 wxauto 的 UIA 方案失效的原因。

实测（微信 4.1.12.26 / WMPF）发现：**热激活 Qt accessibility gate 后**，
UIA 树会立即物化为 ``mmui::MainWindow``，其中：
  * 搜索框 ``mmui::XValidatorTextEdit``（Name=「搜索」）可定位；
  * 输入关键词后下拉 ``AutomationId=search_list`` 暴露结果列表
    （真实结果 aid 前缀 ``search_item_*``），点击可打开会话；
  * 打开会话后 ``AutomationId=chat_input_field`` 输入框可定位，
    其 ``.Name`` 即当前聊天对象（三重校验依据）。

相比坐标 + OCR（``wechatauto.guia``），UIA 路径更准（无 OCR 抖动/生僻字
问题）、更快（无放大/多轮投票）。本模块将其封装为与 guia 相同的调用面，
供 :meth:`WeChatGUI.open_chat` / 发送方法**优先尝试、失败自动降级到 OCR**
（见 guia.py 的混合驱动逻辑）。

热激活安全性：只写运行中 Weixin 进程的 1 个 Qt accessibility byte
（从 Weixin.dll 扫描 RVA，字节码模式匹配 + qt.accessibility.core 引用
距离过滤），不注入代码、不重启进程；该 byte 本就是系统读屏器会写入的
状态位。微信升级后扫描逻辑通常仍有效，硬编码版本表仅作兜底。

使用前提：``pip install uiautomation pywin32``。
"""

from __future__ import annotations

import ctypes
import os
import re
import struct
import threading
import time
from ctypes import wintypes
from functools import lru_cache
from typing import List, Optional, Tuple

import uiautomation as auto

try:
    import win32gui, win32con, win32process, win32api
    _HAS_WIN32 = True
except Exception:                                   # pragma: no cover
    _HAS_WIN32 = False

from wechatauto.logger import wxlog

# ---------------------------------------------------------------------------
# 控件锚点
# ---------------------------------------------------------------------------
MAIN_CLASS = "mmui::MainWindow"
LOGIN_CLASS = "mmui::LoginWindow"
MAIN_NAMES = ("微信", "Weixin")
LOGIN_BTN_NAMES = ("进入微信", "登录", "进入")
LOGIN_OUTLINE_CLASS = "mmui::XOutlineButton"
SEARCH_EDIT_CLASS = "mmui::XValidatorTextEdit"
SEARCH_EDIT_NAME = "搜索"
SESSION_LIST_AID = "session_list"
SEARCH_LIST_AID = "search_list"
RESULT_AID_PREFIX = "search_item_"                 # 真实可打开结果的 aid 前缀
CHAT_INPUT_AID = "chat_input_field"                # 输入框；其 .Name == 当前聊天对象
DEFAULT_EXE = r"C:\Program Files\Tencent\Weixin\Weixin.exe"

# 搜索结果分区标题（aid 为空且名字命中此集合的才算分区头，其余空 aid 视为建议项）
SECTION_HEADERS = {
    "最常使用", "最近使用", "联系人", "群聊", "公众号", "服务号", "订阅号",
    "聊天记录", "收藏", "功能", "小程序", "最近使用过的小程序", "视频号",
    "企业微信联系人", "朋友圈", "搜索网络结果", "企业微信",
}

# ---------------------------------------------------------------------------
# 无障碍“屏幕阅读器”系统标志 / Qt accessibility gate
# ---------------------------------------------------------------------------
SPI_GETSCREENREADER = 0x0046
SPI_SETSCREENREADER = 0x0047
SPIF_SENDCHANGE = 0x02

# 微信内置 Qt accessibility gate：QAccessible 查询前会检查一个运行时 active
# byte。冷启动未启用读屏时该 byte 为 0，WM_GETOBJECT 只能拿到 Qt 外壳；
# 热写为 1 后当前进程立即返回 mmui provider，无需重启微信。优先从 Weixin.dll
# 自动扫描该 byte 的 RVA，下面的版本表仅作扫描失败时的兜底。
QACCESSIBLE_ACTIVE_RVA_BY_VERSION = {
    "4.1.11.22": 0x0A1E7DB8,
}
QACCESSIBLE_CORE_STRING = b"qt.accessibility.core"
QACCESSIBLE_GATE_PATTERN = re.compile(
    rb"\x48\x85\xc9\x0f\x84....\x80\x3d(?P<disp>.{4})"
    rb"\x00\x0f\x84",
    re.DOTALL,
)
IMAGE_SCN_MEM_EXECUTE = 0x20000000
IMAGE_SCN_MEM_WRITE = 0x80000000
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_MODULE_NAME32 = 255
MAX_PATH = 260


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("th32ModuleID", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("GlblcntUsage", ctypes.c_ulong),
        ("ProccntUsage", ctypes.c_ulong),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_ubyte)),
        ("modBaseSize", ctypes.c_ulong),
        ("hModule", ctypes.c_void_p),
        ("szModule", ctypes.c_wchar * (MAX_MODULE_NAME32 + 1)),
        ("szExePath", ctypes.c_wchar * MAX_PATH),
    ]


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class UIATimeout(Exception):
    """UIA 操作超时。"""


class UIANotAvailable(Exception):
    """UIA 树不可用（热激活失败 / 微信未运行等），调用方应降级到 OCR。"""


# ---------------------------------------------------------------------------
# 共享 WeChatDB（密钥提取/建连很慢，进程内只做一次）
# ---------------------------------------------------------------------------
_SHARED_DB = None
_SHARED_DB_LOCK = threading.Lock()


def _shared_db():
    """惰性创建并复用 WeChatDB 单例。

    背景：``WeChatDB()`` 首次构造要提取密钥并打开多个加密库，耗时可达数秒。
    旧实现每次 ``_resolve_search_keyword`` 都新建一个，导致「微信已到前台，
    但搜索框迟迟不出现文字」——发送链路上最大的一段空白等待。
    """
    global _SHARED_DB
    if _SHARED_DB is not None:
        return _SHARED_DB
    with _SHARED_DB_LOCK:
        if _SHARED_DB is None:
            from wechatauto.db import WeChatDB
            _SHARED_DB = WeChatDB()
    return _SHARED_DB


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------

class WeChatUIA:
    """基于 UIAutomation 的微信 4.x 驱动引擎（混合驱动首选路径）。

    用法（一般通过 WeChatGUI 自动切换，也可独立使用）：:

        from wechatauto.uia_driver import WeChatUIA
        uia = WeChatUIA()
        uia.ensure_window()
        uia.open_chat('文件传输助手')
        uia.send_text('你好')
    """

    def __init__(self, timeout: float = 15.0, search_timeout: float = 2.0):
        self.timeout = timeout
        self._win = None
        # 输入框控件缓存：open_chat 成功后 send_text / current_chat 直接复用，
        # 免去每次都在主窗口整棵 UIA 树里深扫。窗口句柄变化或控件失效时重建。
        self._cached_input = None
        self._cached_input_hwnd = 0
        # 线程安全：WeChatBot 等宿主可能在后台线程实例化本驱动，COM 未初始化
        # 时 uiautomation 会报「尚未调用 CoInitialize / 无法加载
        # UIAutomationCore.dll」。CoInitializeEx 幂等，主线程重复调用无害。
        try:
            auto.InitializeUIAutomationInCurrentThread()
        except Exception:
            pass
        try:
            auto.SetGlobalSearchTimeout(search_timeout)
        except Exception:
            pass

    # ------------------------------------------------------------------ 基础
    @staticmethod
    def is_running() -> bool:
        try:
            import subprocess
            out = subprocess.run(["tasklist", "/fi", "imagename eq Weixin.exe",
                                  "/nh"], capture_output=True, text=True,
                                 timeout=10).stdout or ""
            return "Weixin.exe" in out
        except Exception:
            return False

    def wake(self) -> None:
        """weixin:// 协议唤起/显示窗口（托盘态也能拉起）；失败则拉起 exe。"""
        try:
            os.startfile("weixin://")
            return
        except Exception:
            pass
        try:
            import subprocess
            subprocess.Popen([DEFAULT_EXE])
        except Exception:
            wxlog.warning("拉起微信失败")

    # ------------------------------------------------------------------ 热激活
    @staticmethod
    def _set_screen_reader_flag(enable: bool) -> None:
        try:
            ctypes.windll.user32.SystemParametersInfoW(
                SPI_SETSCREENREADER, 1 if enable else 0, 0, SPIF_SENDCHANGE)
        except Exception:
            pass

    @staticmethod
    def _pid_from_hwnd(hwnd: int) -> Optional[int]:
        if _HAS_WIN32:
            try:
                return win32process.GetWindowThreadProcessId(hwnd)[1]
            except Exception:
                return None
        pid = wintypes.DWORD()
        try:
            ctypes.windll.user32.GetWindowThreadProcessId(
                wintypes.HWND(hwnd), ctypes.byref(pid))
            return int(pid.value) or None
        except Exception:
            return None

    @staticmethod
    def _process_modules(pid: int):
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
        kernel32.Module32FirstW.restype = wintypes.BOOL
        kernel32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
        kernel32.Module32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        snapshot = kernel32.CreateToolhelp32Snapshot(
            TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
        if snapshot == INVALID_HANDLE_VALUE:
            return
        try:
            entry = MODULEENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            if not kernel32.Module32FirstW(snapshot, ctypes.byref(entry)):
                return
            while True:
                base = ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0
                yield base, int(entry.modBaseSize), entry.szModule, entry.szExePath
                if not kernel32.Module32NextW(snapshot, ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(snapshot)

    @classmethod
    def _weixin_dll_module(cls, pid: int):
        for base, size, name, path in cls._process_modules(pid) or ():
            if name.lower() == "weixin.dll":
                return base, size, path
        return None

    @staticmethod
    def _pe_sections(data: bytes):
        try:
            pe_off = struct.unpack_from("<I", data, 0x3C)[0]
            if data[pe_off:pe_off + 4] != b"PE\0\0":
                return []
            coff = pe_off + 4
            count = struct.unpack_from("<H", data, coff + 2)[0]
            opt_size = struct.unpack_from("<H", data, coff + 16)[0]
            sec_off = coff + 20 + opt_size
            sections = []
            for i in range(count):
                off = sec_off + i * 40
                name = data[off:off + 8].split(b"\0", 1)[0].decode("ascii", "ignore")
                virtual_size, virtual_address, raw_size, raw_ptr = struct.unpack_from(
                    "<IIII", data, off + 8)
                characteristics = struct.unpack_from("<I", data, off + 36)[0]
                sections.append({
                    "name": name,
                    "rva": virtual_address,
                    "vsize": virtual_size,
                    "raw_size": raw_size,
                    "raw_ptr": raw_ptr,
                    "chars": characteristics,
                })
            return sections
        except Exception:
            return []

    @staticmethod
    def _section_for_rva(sections, rva: int):
        for sec in sections:
            size = max(sec["vsize"], sec["raw_size"])
            if sec["rva"] <= rva < sec["rva"] + size:
                return sec
        return None

    @staticmethod
    def _offset_to_rva(sections, offset: int) -> Optional[int]:
        for sec in sections:
            if sec["raw_ptr"] <= offset < sec["raw_ptr"] + sec["raw_size"]:
                return sec["rva"] + offset - sec["raw_ptr"]
        return None

    @staticmethod
    def _rip_xrefs_to_rva(data: bytes, sections, target_rva: int) -> List[int]:
        xrefs: List[int] = []
        for sec in sections:
            if not (sec["chars"] & IMAGE_SCN_MEM_EXECUTE):
                continue
            start = sec["raw_ptr"]
            end = min(len(data), start + sec["raw_size"])
            raw = data[start:end]
            for i in range(0, max(0, len(raw) - 7)):
                if 0x40 <= raw[i] <= 0x4F and raw[i + 1] == 0x8D and (raw[i + 2] & 0xC7) == 0x05:
                    disp = struct.unpack_from("<i", raw, i + 3)[0]
                    insn_rva = sec["rva"] + i
                    if insn_rva + 7 + disp == target_rva:
                        xrefs.append(insn_rva)
                if raw[i] == 0x8D and (raw[i + 1] & 0xC7) == 0x05:
                    disp = struct.unpack_from("<i", raw, i + 2)[0]
                    insn_rva = sec["rva"] + i
                    if insn_rva + 6 + disp == target_rva:
                        xrefs.append(insn_rva)
        return xrefs

    @staticmethod
    @lru_cache(maxsize=8)
    def _scan_qaccessible_active_rva(dll_path: str) -> Optional[int]:
        try:
            with open(dll_path, "rb") as f:
                data = f.read()
        except OSError:
            return None

        sections = WeChatUIA._pe_sections(data)
        if not sections:
            return None

        core_off = data.find(QACCESSIBLE_CORE_STRING)
        core_rva = WeChatUIA._offset_to_rva(sections, core_off) if core_off >= 0 else None
        core_xrefs = (WeChatUIA._rip_xrefs_to_rva(data, sections, core_rva)
                      if core_rva is not None else [])

        candidates: List[Tuple[int, int]] = []
        for match in QACCESSIBLE_GATE_PATTERN.finditer(data):
            match_rva = WeChatUIA._offset_to_rva(sections, match.start())
            disp_rva = WeChatUIA._offset_to_rva(sections, match.start("disp"))
            if match_rva is None or disp_rva is None:
                continue
            match_sec = WeChatUIA._section_for_rva(sections, match_rva)
            if not match_sec or not (match_sec["chars"] & IMAGE_SCN_MEM_EXECUTE):
                continue

            cmp_rva = disp_rva - 2              # 80 3d <disp32> 00
            disp = struct.unpack("<i", match.group("disp"))[0]
            target_rva = cmp_rva + 7 + disp
            target_sec = WeChatUIA._section_for_rva(sections, target_rva)
            if not target_sec or not (target_sec["chars"] & IMAGE_SCN_MEM_WRITE):
                continue

            if core_xrefs:
                distance = min(abs(match_rva - xref) for xref in core_xrefs)
                # 真正的 QAccessible gate 与 qt.accessibility.core 日志分类
                # 处于同一局部 Qt accessibility 代码岛内
                if distance > 0x20000:
                    continue
            else:
                distance = 0x7FFFFFFF
            candidates.append((distance, target_rva))

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    @staticmethod
    def _qaccessible_active_rva(dll_path: str) -> Optional[int]:
        scanned = WeChatUIA._scan_qaccessible_active_rva(dll_path)
        if scanned is not None:
            return scanned
        version = os.path.basename(os.path.dirname(dll_path))
        return QACCESSIBLE_ACTIVE_RVA_BY_VERSION.get(version)

    @staticmethod
    def _read_process_byte(handle, address: int) -> Optional[int]:
        ctypes.windll.kernel32.ReadProcessMemory.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        ctypes.windll.kernel32.ReadProcessMemory.restype = wintypes.BOOL
        buf = (ctypes.c_ubyte * 1)()
        read = ctypes.c_size_t(0)
        ok = ctypes.windll.kernel32.ReadProcessMemory(
            handle, ctypes.c_void_p(address), buf, 1, ctypes.byref(read))
        return int(buf[0]) if ok and read.value == 1 else None

    @staticmethod
    def _write_process_byte(handle, address: int, value: int) -> bool:
        ctypes.windll.kernel32.WriteProcessMemory.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        ctypes.windll.kernel32.WriteProcessMemory.restype = wintypes.BOOL
        buf = (ctypes.c_ubyte * 1)(value & 0xFF)
        written = ctypes.c_size_t(0)
        ok = ctypes.windll.kernel32.WriteProcessMemory(
            handle, ctypes.c_void_p(address), buf, 1, ctypes.byref(written))
        return bool(ok and written.value == 1)

    def _hot_activate_accessibility(self, hwnd: int) -> bool:
        """运行中热激活 Qt accessibility gate，不重启微信进程。"""
        pid = self._pid_from_hwnd(hwnd)
        if not pid:
            return False
        mod = self._weixin_dll_module(pid)
        if not mod:
            wxlog.warning("热激活 UIA 失败：PID %s 未找到 Weixin.dll。", pid)
            return False
        base, _size, dll_path = mod
        rva = self._qaccessible_active_rva(dll_path)
        if rva is None:
            wxlog.warning("热激活 UIA 失败：不支持的 Weixin.dll 版本路径 %s。", dll_path)
            return False

        access = (PROCESS_QUERY_INFORMATION | PROCESS_VM_READ |
                  PROCESS_VM_WRITE | PROCESS_VM_OPERATION)
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(access, False, pid)
        if not handle:
            wxlog.warning("热激活 UIA 失败：无法打开 Weixin.exe PID %s。", pid)
            return False
        try:
            address = int(base) + int(rva)
            current = self._read_process_byte(handle, address)
            if current == 1:
                return True
            if current is None:
                wxlog.warning("热激活 UIA 失败：无法读取 active byte。")
                return False
            if not self._write_process_byte(handle, address, 1):
                wxlog.warning("热激活 UIA 失败：无法写入 active byte。")
                return False
            wxlog.info("已热激活微信 UIA 树：PID=%s Weixin.dll+0x%x: %s -> 1",
                       pid, rva, current)
            return True
        finally:
            kernel32.CloseHandle(handle)

    def _wake_accessibility(self) -> bool:
        ok = False
        for hwnd in self._wechat_hwnds():
            ok = self._hot_activate_accessibility(hwnd) or ok
        if ok:
            self._win = None
            time.sleep(0.2)
        return ok

    # ------------------------------------------------------------------ 窗口定位
    def _wechat_hwnds(self):
        if not _HAS_WIN32:
            return []
        res = []

        def cb(h, _):
            try:
                if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) in MAIN_NAMES:
                    # 只保留加载了 Weixin.dll 的主进程窗口，过滤掉无 DLL 的
                    # 辅助进程窗口（其热激活必然失败，只会产生噪音警告）
                    pid = self._pid_from_hwnd(h)
                    if pid and self._weixin_dll_module(pid):
                        l, t, r, b = win32gui.GetWindowRect(h)
                        res.append(((r - l) * (b - t), h))
            except Exception:
                pass
            return True
        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            pass
        res.sort(reverse=True)
        return [h for _, h in res]

    def _anchor(self, hwnd):
        try:
            return auto.ControlFromHandle(hwnd)
        except Exception:
            return None

    def _find_main(self):
        for h in self._wechat_hwnds():
            c = self._anchor(h)
            if c is not None and (c.ClassName or "") == MAIN_CLASS:
                return c
        return None

    def _login_window(self):
        for h in self._wechat_hwnds():
            c = self._anchor(h)
            if c is not None and (c.ClassName or "") == LOGIN_CLASS:
                return c
        return None

    # ------------------------------------------------------------------ 前台/登录
    @staticmethod
    def _force_foreground(hwnd) -> bool:
        if not _HAS_WIN32 or not hwnd:
            return False
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        except Exception:
            pass
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        cur = win32api.GetCurrentThreadId()
        fg = win32gui.GetForegroundWindow()
        fgt = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
        try:
            if fgt:
                win32process.AttachThreadInput(cur, fgt, True)
        except Exception:
            pass
        ok = False
        for _ in range(3):
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                win32gui.BringWindowToTop(hwnd)
                win32gui.SetForegroundWindow(hwnd)
                if win32gui.GetForegroundWindow() == hwnd:
                    ok = True
                    break
            except Exception:
                pass
            time.sleep(0.1)
        try:
            if fgt:
                win32process.AttachThreadInput(cur, fgt, False)
        except Exception:
            pass
        return ok

    def _activate(self, win) -> None:
        if not self._force_foreground(win.NativeWindowHandle):
            try:
                win.ShowWindow(9)
            except Exception:
                pass
            try:
                win.SetActive()
            except Exception:
                pass
        time.sleep(0.25)

    def _wait_main(self, timeout: float, allow_login: bool = True,
                   allow_accessibility_wake: bool = True):
        deadline = time.time() + max(timeout, 15)
        last_wake = time.time()
        accessibility_woke = False
        while time.time() < deadline:
            if allow_login:
                self._auto_login()
            w = self._find_main()
            if w is not None:
                self._win = w
                self._activate(w)
                return w
            if (allow_accessibility_wake and not accessibility_woke
                    and self._login_window() is None and self._wechat_hwnds()
                    and time.time() - last_wake > 3):
                self._wake_accessibility()
                accessibility_woke = True
                last_wake = time.time()
                continue
            if self._login_window() is None and time.time() - last_wake > 6:
                self.wake()
                last_wake = time.time()
            time.sleep(0.8)
        raise UIATimeout("等待微信主窗口超时（客户端未就绪）。")

    def ensure_window(self, wake: bool = True, timeout: Optional[float] = None) -> bool:
        """确保可访问的主窗口存在并置前，返回是否成功。

        优先 ControlFromHandle 按句柄锚定拿 mmui 树；若树未物化（只剩 Qt
        外壳）则对当前 Weixin 进程热激活，不重启微信。任一环节失败返回
        False，由调用方降级到 OCR。
        """
        timeout = timeout or self.timeout
        w = self._find_main()
        if w is not None:
            self._win = w
            self._activate(w)
            return True
        if not wake:
            return False
        try:
            if self._login_window() is not None:
                self._auto_login()
            elif self._wechat_hwnds():
                self._wake_accessibility()
            else:
                self._set_screen_reader_flag(True)
                self.wake()
            self._wait_main(timeout)
            return self._win is not None
        except UIATimeout:
            return False
        except Exception as e:
            wxlog.debug("ensure_window 失败：%s", e)
            return False

    def _auto_login(self) -> bool:
        lw = self._login_window()
        if lw is None:
            return False
        self._force_foreground(lw.NativeWindowHandle)
        time.sleep(0.3)
        for nm in LOGIN_BTN_NAMES:
            btn = lw.ButtonControl(Name=nm)
            if btn.Exists(0.2, 0.1):
                btn.Click()
                time.sleep(1.2)
                return True
        ob = lw.ButtonControl(ClassName=LOGIN_OUTLINE_CLASS)
        if ob.Exists(0.2, 0.1) and (ob.Name or "").strip():
            ob.Click()
            time.sleep(1.2)
            return True
        return False

    # ------------------------------------------------------------------ 剪贴板
    @staticmethod
    def _clip_get() -> str:
        try:
            import pyperclip
            return pyperclip.paste()
        except Exception:
            pass
        try:
            return auto.GetClipboardText()
        except Exception:
            return ""

    @staticmethod
    def _clip_set(text: str) -> None:
        try:
            import pyperclip
            pyperclip.copy(text)
            return
        except Exception:
            pass
        try:
            auto.SetClipboardText(text)
        except Exception:
            pass

    @staticmethod
    def _set_cursor(x: int, y: int) -> None:
        try:
            import ctypes
            ctypes.windll.user32.SetCursorPos(int(x), int(y))
        except Exception:
            pass

    @staticmethod
    def _mouse_wheel(delta: int) -> None:
        try:
            import ctypes
            ctypes.windll.user32.mouse_event(0x0800, 0, 0, int(delta), 0)
        except Exception:
            pass

    @staticmethod
    def _left_click() -> None:
        try:
            import ctypes
            ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # down
            time.sleep(0.05)
            ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # up
        except Exception:
            pass

    @staticmethod
    def _right_click() -> None:
        try:
            import ctypes
            ctypes.windll.user32.mouse_event(0x0008, 0, 0, 0, 0)  # down
            time.sleep(0.05)
            ctypes.windll.user32.mouse_event(0x0010, 0, 0, 0, 0)  # up
        except Exception:
            pass

    # ------------------------------------------------------------------ 控件定位
    def _search_box(self, win):
        for kw in (dict(ClassName=SEARCH_EDIT_CLASS, Name=SEARCH_EDIT_NAME),
                   dict(Name=SEARCH_EDIT_NAME),
                   dict(ClassName=SEARCH_EDIT_CLASS)):
            e = win.EditControl(**kw)
            if e.Exists(1.0, 0.2):
                return e
        return None

    def _chat_input(self, win=None):
        win = win or self._win
        if win is None:
            return None
        # 缓存命中：同窗口且控件仍存活（Exists(0) 是一次轻量探测），
        # 跳过整树深扫。会话切换时控件对象不变（仅 Name 变化），可安全复用。
        cached = self._cached_input
        if (cached is not None
                and self._cached_input_hwnd == (win.NativeWindowHandle or 0)):
            try:
                if cached.Exists(0, 0):
                    return cached
            except Exception:
                pass
            self._cached_input = None
        e = win.EditControl(AutomationId=CHAT_INPUT_AID)
        if e.Exists(1.0, 0.2):
            self._cached_input = e
            self._cached_input_hwnd = win.NativeWindowHandle or 0
            return e
        return None

    def current_chat(self) -> Optional[str]:
        e = self._chat_input()
        return (e.Name or None) if e else None

    def _find_search_list(self, timeout: float = 3.0):
        deadline = time.time() + timeout
        win = self._win
        while time.time() < deadline:
            # 优先限定在主窗口子树内查找：从桌面根全局深搜会枚举所有
            # 顶层窗口的无障碍树（实测慢约 10 倍，微信 4.x 树极大）
            if win is not None:
                try:
                    lst = win.ListControl(AutomationId=SEARCH_LIST_AID)
                    if lst.Exists(0.2, 0.1):
                        return lst
                except Exception:
                    pass
            # 兜底：搜索下拉若独立于主窗口（弹窗场景），仍从桌面根找
            try:
                lst = auto.ListControl(searchDepth=0xFFFFFFFF,
                                       AutomationId=SEARCH_LIST_AID)
                if lst.Exists(0.2, 0.1):
                    return lst
            except Exception:
                pass
            time.sleep(0.2)
        return None

    def _collect_results(self, keyword: str, settle: float = 1.5) -> List[dict]:
        """采集去重后的候选 [{name, section, aid, cell}]。"""
        lst = self._find_search_list()
        if lst is None:
            return []
        deadline = time.time() + settle
        raw = []
        while time.time() < deadline:
            section = None
            raw = []
            for c in lst.GetChildren():
                name = c.Name or ""
                aid = c.AutomationId or ""
                if not aid:
                    head = name.strip()
                    if head in SECTION_HEADERS:
                        section = head
                    continue
                if not aid.startswith(RESULT_AID_PREFIX):
                    continue
                raw.append({
                    "cell": c,
                    "name": name.split("\n", 1)[0].strip(),
                    # 副标题（第二行起的其余文本）：通常是「企业微信」
                    # 「微信号: xxx」或最近消息摘要。留作同名联系人区分，
                    # 为空时不影响既有逻辑。
                    "sub": " ".join(
                        p.strip() for p in name.split("\n")[1:] if p.strip()),
                    "section": section,
                    "aid": aid,
                })
            if raw:
                break
            time.sleep(0.2)

        kw = keyword.strip()
        matched = [r for r in raw if r["name"] == kw]
        if not matched:
            matched = raw[:]

        # 最常使用/最近使用中重复出现的同一 aid 去重
        non_freq = {r["aid"] for r in matched
                    if r["section"] not in ("最常使用", "最近使用")}
        pruned = [r for r in matched
                  if not (r["section"] in ("最常使用", "最近使用")
                          and r["aid"] in non_freq)]
        for i, r in enumerate(pruned):
            r["index"] = i
        return pruned

    # ------------------------------------------------------------------ 高层动作
    @staticmethod
    def _is_system_id(text: str) -> bool:
        """是否微信系统账号（wxid_/gh_/@chatroom）——这类不能直接用于搜索框。"""
        t = (text or "").strip().lower()
        return (t.startswith("wxid_") or t.startswith("gh_")
                or t.endswith("@chatroom") or "@" in t)

    @staticmethod
    def _resolve_search_keyword(keyword: str) -> List[str]:
        """把账号/username 解析成微信搜索框能命中的关键词列表。

        微信搜索框本身就能命中昵称/备注/微信号，只有传入 wxid 这类系统
        账号时才需要查库映射。**查库是整条发送链路里最慢的一步**（要提取
        密钥 + 打开加密库），而 99% 的调用传的都是会话显示名，因此显示名
        直接原样搜索，不再碰数据库；wxid 场景复用进程内单例连接。
        """
        kw = (keyword or "").strip()
        if not kw:
            return []
        if not WeChatUIA._is_system_id(kw):
            return [kw]
        candidates = [kw]
        try:
            db = _shared_db()
            for hit in db.search_contact(kw):
                for k in (hit.get("remark"), hit.get("nick_name")):
                    if k and k not in candidates:
                        candidates.append(k)
        except Exception:
            pass
        # 企业微信/客服账号（xxx@openim、@kefu）：这类 ID 在微信搜索框里
        # 不是有效关键词，先搜它必然空等一轮重试（约 2.4s）才轮到昵称。
        # 因此把昵称提前、ID 退到末尾仅作兜底。
        low = kw.lower()
        if low.endswith("@openim") or "@kefu" in low:
            candidates = [c for c in candidates if c != kw] + [kw]
        return candidates

    def open_chat(self, keyword: str, index: Optional[int] = None,
                  section: Optional[str] = None, retries: int = 2) -> bool:
        """搜索并打开联系人/群聊，成功后校验输入框 Name。返回是否成功。

        传入 username/wxid 时自动映射为昵称/备注/微信号再搜索（微信搜索框
        不认 wxid）。搜索框残留会影响命中，每次打开前先清空重试。
        """
        if not self.ensure_window():
            return False
        win = self._win
        box = self._search_box(win)
        if box is None:
            return False

        keywords = self._resolve_search_keyword(keyword)
        results = []
        used_kw = keyword
        for kw in keywords:
            got = False
            for attempt in range(max(1, retries)):
                self._paste_into(box, kw, clear=True)
                time.sleep(0.8)
                got = self._collect_results(kw)
                if got:
                    break
                time.sleep(0.4)
            if got:
                results = got
                used_kw = kw
                break

        if not results:
            return False

        if section:
            filtered = [r for r in results
                        if (r["section"] or "") == section]
            if filtered:
                results = filtered
        if len(results) > 1 and index is not None:
            filtered = [r for r in results if r["index"] == index]
            if filtered:
                results = filtered
        elif len(results) > 1:
            # 有精确名称命中时优先精确命中，否则用第一个
            exact = [r for r in results if r["name"] == used_kw]
            if exact:
                results = exact
        # 企业微信/客服联系人常与普通好友同名（同一个人的两个身份），
        # 搜索结果里只靠名称无法区分；此时优先副标题标注「企业微信」的
        # 那一条。副标题缺失或都不含该字样时保持原有取第一条行为。
        low_kw = (keyword or "").strip().lower()
        if len(results) > 1 and (low_kw.endswith("@openim")
                                or "@kefu" in low_kw):
            wx = [r for r in results if "企业微信" in (r.get("sub") or "")]
            if wx:
                results = wx

        chosen = results[0]
        chosen["cell"].Click()
        time.sleep(0.7)

        name = self.current_chat()
        # same_chat 容忍群名「(123)」后缀与空格差异：严格相等会把
        # 「搜索点击已成功」误判为失败，导致调用方落入十几秒的 OCR 兜底
        if name and (self.same_chat(name, chosen["name"])
                     or self.same_chat(name, used_kw)):
            return True
        return False

    def send_text(self, text: str) -> bool:
        """在已打开会话的输入框发送文本。返回是否成功。"""
        if not self.ensure_window():
            return False
        e = self._chat_input()
        if e is None:
            return False
        self._paste_into(e, text, clear=True)
        time.sleep(0.2)
        try:
            e.SendKeys("{Enter}", waitTime=0.05)
        except Exception:
            return False
        return True

    def send_text_to(self, text: str, who: str) -> bool:
        """打开会话并发送文本（组合动作）。"""
        if self.same_chat(self.current_chat(), who):
            return self.send_text(text)
        if not self.open_chat(who):
            return False
        return self.send_text(text)

    # ------------------------------------------------------------------ 会话比较
    @staticmethod
    def same_chat(current: Optional[str], target: str) -> bool:
        """判断当前输入框所属会话是否就是目标会话（容忍群名带人数后缀）。

        微信群聊输入框 ``Name`` 可能带「(123)」成员数后缀或空格差异，
        严格相等会导致每发一条都重新搜索打开会话（数秒级浪费）。
        """
        if not current or not target:
            return False
        a = re.sub(r"\s*[（(]\d+[)）]\s*$", "", str(current)).strip()
        b = re.sub(r"\s*[（(]\d+[)）]\s*$", "", str(target)).strip()
        if not a or not b:
            return False
        return a == b or a.startswith(b) or b.startswith(a)

    # ------------------------------------------------------------------ 引用 / @
    def _right_click_row(self, row) -> bool:
        """在消息行内容重心处右键（self 靠右、好友靠左），弹操作菜单。"""
        try:
            r = row.BoundingRectangle
            if r.right - r.left < 10 or r.bottom - r.top < 10:
                return False
            from PIL import ImageGrab as IG
            import numpy as np
            img = IG.grab(bbox=(r.left, r.top, r.right, r.bottom))
            arr = np.array(img.convert("RGB"))
            bg = (arr.max(axis=2) > 235) & ((arr.max(axis=2) - arr.min(axis=2)) < 22)
            colc = (~bg).sum(axis=0)
            total = int(colc.sum())
            if total >= 20:
                cx = int(r.left + sum(x * colc[x] for x in range(len(colc))) / total)
            else:
                cx = (r.left + r.right) // 2
            cy = (r.top + r.bottom) // 2
            self._set_cursor(cx, cy)
            time.sleep(0.2)
            self._right_click()
            time.sleep(0.8)
            return True
        except Exception as exc:
            wxlog.debug(f"右键消息行失败：{exc}")
            return False

    def _find_row_by_text(self, target_text: str):
        """在消息列表中按文本查找消息行控件（先可视区，再滚向历史）。"""
        text = (target_text or "").strip()
        if not text:
            return None
        lst = self._message_list()
        if lst is None:
            return None
        # 先只在可视区找：绝大多数引用目标就是最近几条
        for ch in list(lst.GetChildren()):
            try:
                nm = (ch.Name or "")
                if text in nm:
                    return ch
            except Exception:
                continue
        got = self.find_in_message_list(
            lambda cn, nm: text in (nm or ""), max_scrolls=12)
        return got[2] if got else None

    def _menu_click(self, keywords: Tuple[str, ...]) -> bool:
        """在当前弹出的右键菜单里点击名称含任一关键词的菜单项。"""
        for kw in keywords:
            item = self._uia_find_menu_item(kw)
            if item is not None and self._uia_click_menu_item(item):
                return True
        return False

    def _focus_input_end(self, ctrl) -> None:
        """把光标放到输入框底部（避开表情/文件工具栏，且不选中既有内容）。"""
        try:
            r = ctrl.BoundingRectangle
            self._set_cursor((r.left + r.right) // 2, r.bottom - 12)
            time.sleep(0.15)
            self._left_click()
            time.sleep(0.15)
        except Exception:
            pass

    def _paste_append(self, ctrl, text: str) -> bool:
        """在现有草稿后追加文本（不 Ctrl+A/Delete，避免清掉引用卡片或 @ 提及）。"""
        self._clip_set(text)
        try:
            ctrl.SendKeys("{Ctrl}v", waitTime=0.05)
            return True
        except Exception:
            return False

    def _clear_input(self, ctrl) -> None:
        """清空输入框草稿（插入引用卡片/@ 提及之前调用）。"""
        try:
            self._focus_input_end(ctrl)
            ctrl.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
        except Exception:
            pass

    def quote_message(self, text: str, who: Optional[str] = None,
                      target_text: Optional[str] = None) -> bool:
        """引用消息后发送正文（UIA 路线：右键消息行 → 菜单「引用」）。

        相比 OCR 方案（靠识别到的文字去匹配、再识别菜单文字点击），UIA
        菜单项是真实控件，不受字号/生僻字/深色主题影响；图片、小程序等
        「没有可识别文字」的消息也能正确引用。

        Args:
            text: 要发送的正文
            who: 目标会话（None = 当前已打开会话）
            target_text: 要引用的消息文本片段；None = 引用最新一条
        """
        if not self.ensure_window():
            return False
        if who and not self.same_chat(self.current_chat(), who):
            if not self.open_chat(who):
                return False
            time.sleep(0.5)
        # 先清掉旧草稿（此时还没有引用卡片，可以安全清空）
        e0 = self._chat_input()
        if e0 is not None:
            self._clear_input(e0)
        if target_text:
            row = self._find_row_by_text(target_text)
            if row is None:
                wxlog.debug(f"引用目标未在消息列表中定位到：{target_text[:30]}")
                return False
            pos = self._right_click_row(row)
        else:
            pos = self._right_click_latest_row(None)
        if not pos:
            return False
        if not self._menu_click(("引用", "Quote")):
            # 菜单没弹出来时再右键一次，避免首击落在消息空白处
            self._set_cursor(*pos)
            time.sleep(0.2)
            self._right_click()
            time.sleep(0.8)
            if not self._menu_click(("引用", "Quote")):
                try:
                    from wechatauto.ui.component import Menu
                    Menu(self).close()
                except Exception:
                    pass
                return False
        time.sleep(0.5)
        e = self._chat_input()
        if e is None:
            return False
        # 点击菜单后焦点可能落在消息区：先把光标放回输入框末尾
        self._focus_input_end(e)
        # 注意：草稿里已经有「引用卡片」，这里绝不能再 Ctrl+A/Delete
        # （那会把引用块一起清掉，变成普通消息）
        if not self._paste_append(e, text):
            return False
        time.sleep(0.2)
        try:
            e.SendKeys("{Enter}", waitTime=0.05)
        except Exception:
            return False
        return True

    # -- @ 群成员 -------------------------------------------------------
    def _mention_popup(self, timeout: float = 2.0):
        """定位 @ 成员弹层（mmui::XPopover / AutomationId=MentionPopover）。"""
        deadline = time.time() + timeout
        variants = (
            dict(ClassName="mmui::XPopover", AutomationId="MentionPopover"),
            dict(AutomationId="MentionPopover"),
            dict(ClassName="mmui::XPopover", Name="Weixin"),
        )
        while time.time() < deadline:
            for kw in variants:
                try:
                    pop = self._win.WindowControl(searchDepth=0xFFFFFFFF, **kw)
                    if pop.Exists(0.1, 0.1):
                        return pop
                except Exception:
                    continue
            time.sleep(0.2)
        return None

    def _pick_mention(self, member: str, timeout: float = 2.0) -> bool:
        """在已弹出的 @ 成员列表里选中 member（精确名 → 前缀 → 唯一项）。"""
        pop = self._mention_popup(timeout)
        if pop is None:
            return False
        try:
            lst = pop.ListControl()
            if not lst.Exists(0.2, 0.1):
                return False
            items = [i for i in lst.GetChildren() if (i.Name or "").strip()]
        except Exception as exc:
            wxlog.debug(f"读取 @ 成员列表失败：{exc}")
            return False
        if not items:
            return False
        target = (member or "").strip()
        hit = next((i for i in items if (i.Name or "").strip() == target), None)
        if hit is None:
            hit = next((i for i in items
                        if target and target in (i.Name or "")), None)
        if hit is None and len(items) == 1:
            hit = items[0]
        if hit is None:
            return False
        try:
            auto.RollIntoView(pop, hit)
        except Exception:
            pass
        try:
            hit.Click()
        except Exception:
            return False
        time.sleep(0.25)
        return True

    def at_members(self, members: List[str], text: str,
                   who: Optional[str] = None) -> bool:
        """群聊 @ 多个成员后一次性发送正文（同一条消息）。

        旧实现是「每个成员各发一条消息」，@ 多个成员时会发出多条空正文
        消息；这里改成：逐个 @ 进同一个草稿 → 追加正文 → 只回车一次。
        """
        names = [m.strip() for m in (members or []) if m and m.strip()]
        if not names:
            return self.send_text(text)
        if not self.ensure_window():
            return False
        if who and not self.same_chat(self.current_chat(), who):
            if not self.open_chat(who):
                return False
            time.sleep(0.5)
        e = self._chat_input()
        if e is None:
            return False
        # 清空草稿（可能有上次失败残留）
        try:
            e.Click()
            e.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
        except Exception:
            pass
        for name in names:
            try:
                e.SendKeys("@", waitTime=0.05)
            except Exception:
                return False
            time.sleep(0.45)
            if not self._pick_mention(name):
                wxlog.debug(f"@ 成员未在弹层中选中：{name}")
                # 退一步：输入名字过滤后再选（部分版本 @ 后不弹全量列表）
                try:
                    e.SendKeys(name, waitTime=0.05)
                except Exception:
                    return False
                time.sleep(0.6)
                if not self._pick_mention(name, timeout=1.5):
                    return False
            time.sleep(0.25)
        if text:
            # 直接用 Ctrl+V 追加：不点击输入框，避免光标落回已插入的 @ 提及之前
            if not self._paste_append(e, text):
                return False
            time.sleep(0.2)
        try:
            e.SendKeys("{Enter}", waitTime=0.05)
        except Exception:
            return False
        return True


    def voice_call(self, who: Optional[str] = None, video: bool = False) -> bool:
        """发起语音/视频通话（点击标题栏通话按钮 → 选择菜单项）。

        微信 4.x 标题栏暴露 ``mmui::ChatVoIPView`` 下的 ``voip_button``
        （aid=voip_button）。点击后弹出 ``mmui::XMenuView`` 菜单，含
        「语音通话」「视频通话」两个 ``MenuItemControl``（aid=XMenuItem）；
        需再点击目标菜单项才真正发起通话。video=True 时选「视频通话」，
        否则选「语音通话」。
        """
        if not self.ensure_window():
            return False
        if who and not self.current_chat() == who:
            if not self.open_chat(who):
                return False
        win = self._win
        if win is None:
            return False
        # voip_button 控件树会动态重建，需重试定位
        btn = None
        for _ in range(6):
            try:
                btn = win.ButtonControl(AutomationId="voip_button")
                if btn.Exists(0.6, 0.2):
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not btn or not btn.Exists(0):
            return False
        try:
            btn.Click()
            time.sleep(0.8)
        except Exception:
            return False
        # 菜单里选择 语音/视频 通话项
        target_name = "视频通话" if video else "语音通话"
        for _ in range(4):
            try:
                item = win.MenuItemControl(AutomationId="XMenuItem", Name=target_name)
                if item.Exists(0.5, 0.2):
                    item.Click()
                    time.sleep(0.5)
                    return True
            except Exception:
                pass
            # 菜单可能延迟出现或树重建，重试
            time.sleep(0.4)
        return False

    def _find_friend_row(self):
        """找一条对方（friend）消息行控件，供右键头像触发拍一拍。

        消息行 rect 是全宽，方向需按行内内容重心判断（friend 内容靠左，
        self 靠右）。返回 friend 消息行控件。
        """
        lst = self._message_list()
        if lst is None:
            return None
        try:
            from PIL import ImageGrab as IG
            import numpy as np
        except Exception:
            return None
        # 可视区（消息列表内部矩形），过滤掉滚出可视区的行
        lr = lst.BoundingRectangle
        vis_top, vis_bottom = lr.top, lr.bottom
        # 优先文字行，其次其他内容行（动画表情/图片/引用等）
        rows = list(lst.GetChildren())
        for ch in sorted(rows, key=lambda c: c.ClassName != "mmui::ChatTextItemView"):
            try:
                cn = ch.ClassName or ""
                if not cn.startswith("mmui::Chat"):
                    continue
                if cn in ("mmui::ChatItemView", "mmui::ChatSystemInfoItemView"):
                    continue
                r = ch.BoundingRectangle
                if r.bottom - r.top < 40:
                    continue
                # 行必须在可视区内（含部分露出），排除被遮挡/滚出的行
                if r.top >= vis_bottom or r.bottom <= vis_top:
                    continue
                img = IG.grab(bbox=(r.left, r.top, r.right, r.bottom))
                arr = np.array(img.convert("RGB"))
                bg = (arr.max(axis=2) > 235) & ((arr.max(axis=2) - arr.min(axis=2)) < 22)
                colc = (~bg).sum(axis=0)
                total = int(colc.sum())
                if total < 20:
                    continue
                weighted = sum(x * colc[x] for x in range(len(colc))) / total
                if weighted < arr.shape[1] * 0.5:
                    return ch
            except Exception:
                continue
        return None

    def poke(self, who: Optional[str] = None) -> bool:
        """对联系人发起「拍一拍」（右键头像 → 点击拍一拍菜单项）。

        微信 4.x 的拍一拍只能通过右键聊天中对方头像触发，菜单为自绘
        不暴露 UIA；因此用「右键头像 + 全屏 OCR 定位拍一拍文字」实现。
        需要 winsdk OCR 可用。
        """
        if not self.ensure_window():
            return False
        if who and not self.current_chat() == who:
            if not self.open_chat(who):
                return False
        row = self._find_friend_row()
        if row is None:
            return False
        try:
            r = row.BoundingRectangle
        except Exception:
            return False
        # 头像位于消息行最左侧约 40-50px 处
        ax = r.left + 70
        ay = (r.top + r.bottom) // 2
        try:
            from wechatauto.guia import ScreenOCR
            import PIL.ImageGrab as IG
        except Exception:
            return False
        for attempt in range(2):
            self._set_cursor(ax, ay)
            time.sleep(0.2)
            self._right_click()
            time.sleep(1.0)
            img = IG.grab()
            res = ScreenOCR.recognize(img)
            for text, x, y, w, h in res:
                t = (text or "").replace(" ", "")
                if "拍一拍" in t or t == "拍一" or t.startswith("拍一"):
                    # 点击该文字中心
                    cx = x + w // 2
                    cy = y + h // 2
                    self._set_cursor(cx, cy)
                    time.sleep(0.2)
                    self._left_click()
                    time.sleep(0.5)
                    return True
        return False

    def _right_click_latest_row(self, who: Optional[str] = None) -> Optional[Tuple[int, int]]:
        """打开会话并右键最新一条消息行，返回气泡内右键坐标 (x, y)；失败返回 None。"""
        if not self.ensure_window():
            return None
        if who and not self.current_chat() == who:
            if not self.open_chat(who):
                return None
        lst = self._message_list()
        if lst is None:
            return None
        rows = list(lst.GetChildren())
        # 取可视区内最底部（最新）的消息行；时间分隔行 ChatItemView 跳过
        candidates = []
        for ch in rows:
            try:
                cn = ch.ClassName or ""
                if not cn.startswith("mmui::Chat"):
                    continue
                if cn in ("mmui::ChatItemView", "mmui::ChatSystemInfoItemView"):
                    continue
                r = ch.BoundingRectangle
                if r.bottom - r.top < 40:
                    continue
                candidates.append((ch, r))
            except Exception:
                continue
        if not candidates:
            return None
        # 最新消息在可视区底部（消息列表打开即定位在最新），取 bottom 最大者
        target = max(candidates, key=lambda t: t[1].bottom)
        ch, r = target
        # 消息行内取内容重心 x（self 靠右、friend 靠左），y 取行垂直中心
        try:
            from PIL import ImageGrab as IG
            img = IG.grab(bbox=(r.left, r.top, r.right, r.bottom))
            import numpy as np
            arr = np.array(img.convert("RGB"))
            bg = (arr.max(axis=2) > 235) & ((arr.max(axis=2) - arr.min(axis=2)) < 22)
            colc = (~bg).sum(axis=0)
            total = int(colc.sum())
            if total >= 20:
                cx = sum(x * colc[x] for x in range(len(colc))) / total
                cx = int(r.left + cx)
            else:
                cx = (r.left + r.right) // 2
        except Exception:
            cx = (r.left + r.right) // 2
        cy = (r.top + r.bottom) // 2
        self._set_cursor(cx, cy)
        time.sleep(0.2)
        self._right_click()
        time.sleep(1.0)
        return (cx, cy)

    def _uia_find_menu_item(self, name_sub: str, max_depth: int = 6):
        """在主窗口树内查找菜单项控件（UIA 方案）。

        微信 4.x 右键菜单在热激活后物化为 UIA 节点：``mmui::XMenu`` 下挂
        ``mmui::XMenuView``（Name 即菜单文字）。只遍历主窗口子树，避免
        触发 Windows UIA 根遍历的系统挂起 bug。返回匹配的控件或 None。
        """
        w = self._win
        if w is None:
            return None
        found = [None]

        def walk(c, d=0):
            if found[0] is not None or d > max_depth:
                return
            try:
                children = c.GetChildren()
            except Exception:
                return
            for ch in children:
                try:
                    nm = ch.Name or ""
                    cn = ch.ClassName or ""
                except Exception:
                    nm = cn = ""
                if cn == "mmui::XMenuView" and name_sub in nm:
                    found[0] = ch
                    return
                walk(ch, d + 1)

        try:
            walk(w)
        except Exception:
            return None
        return found[0]

    def _uia_click_menu_item(self, ctrl) -> bool:
        """通过 UIA Invoke/LegacyIAccessible 或鼠标点击菜单项控件。"""
        for method in ("Invoke", "Select", "Expand"):
            try:
                getattr(ctrl, method)()
                time.sleep(0.3)
                return True
            except Exception:
                continue
        try:
            r = ctrl.BoundingRectangle
            cx = (r.left + r.right) // 2
            cy = (r.top + r.bottom) // 2
            self._set_cursor(cx, cy)
            time.sleep(0.2)
            self._left_click()
            time.sleep(0.3)
            return True
        except Exception:
            return False

    def recall_last_message(self, who: Optional[str] = None) -> bool:
        """撤回当前会话最新一条自己发送的消息。

        UIA 方案优先：右键消息行后，主窗口树内 ``mmui::XMenuView`` 已物化
        菜单项（Name 含「撤回」），直接定位点击；若菜单项为「删除」（消息
        超过 2 分钟撤回时限）则返回失败。UIA 不可用/未命中时降级到 OCR
        （全屏识别「撤回」文字定位点击）。两者都失败返回 False。
        """
        pos = self._right_click_latest_row(who)
        if pos is None:
            return False
        # UIA 方案
        item = self._uia_find_menu_item("撤回")
        if item is not None:
            if self._uia_click_menu_item(item):
                return True
        # OCR 兜底
        try:
            from wechatauto.guia import ScreenOCR
            import PIL.ImageGrab as IG
        except Exception:
            return False
        for attempt in range(2):
            if attempt > 0:
                cx, cy = pos
                self._set_cursor(cx, cy)
                time.sleep(0.2)
                self._right_click()
                time.sleep(1.0)
            img = IG.grab()
            res = ScreenOCR.recognize(img)
            for text, x, y, w, h in res:
                t = (text or "").replace(" ", "")
                if "撤回" in t or t == "撤回":
                    cxx = x + w // 2
                    cyy = y + h // 2
                    self._set_cursor(cxx, cyy)
                    time.sleep(0.2)
                    self._left_click()
                    time.sleep(0.5)
                    return True
        return False

    # ------------------------------------------------------------------ 剪贴板粘贴
    def _paste_into(self, ctrl, text: str, clear: bool = True) -> None:
        ctrl.Click()
        time.sleep(0.1)
        if clear:
            try:
                ctrl.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
            except Exception:
                pass
        self._clip_set(text)
        try:
            ctrl.SendKeys("{Ctrl}v", waitTime=0.05)
        except Exception:
            pass

    # ------------------------------------------------------------------ 消息列表定位
    def _message_list(self, win=None):
        """定位当前会话的消息列表控件（RecyclerListView / chat_message_list）。"""
        win = win or self._win
        if win is None:
            return None

        def walk(c, d=0):
            if d > 15:
                return None
            for ch in c.GetChildren():
                try:
                    cn = ch.ClassName or ""
                except Exception:
                    continue
                if cn == "mmui::RecyclerListView":
                    return ch
                r = walk(ch, d + 1)
                if r is not None:
                    return r
            return None

        return walk(win)

    def find_in_message_list(self, predicate, match_last: bool = False,
                             max_scrolls: int = 40) -> Optional[Tuple]:
        """在消息列表中按谓词查找消息控件，返回 (className, name, rect)。

        RecyclerListView 是虚拟化列表，只实例化可视区约 12 条；历史消息需
        滚动。ScrollPattern 不可用（返回空），改用鼠标滚轮驱动列表滚动。
        match_last=True 时滚到底后从底部向上找，用于取最新表情/消息。
        """
        lst = self._message_list()
        if lst is None:
            return None

        def scroll(direction: str, times: int = 1) -> None:
            # direction: 'up'=滚向最新(底部), 'down'=滚向历史(更早)
            # 实测（微信4.1.12.26）：open_chat 后消息列表定位在底部(最新)，
            # 滚轮 +120 滚向历史(更早)，-120 滚向最新。
            try:
                r = lst.BoundingRectangle
                cx = (r.left + r.right) // 2
                cy = (r.top + r.bottom) // 2
                self._set_cursor(cx, cy)
                delta = -120 if direction == 'up' else 120
                for _ in range(times):
                    self._mouse_wheel(delta)
                    time.sleep(0.2)
            except Exception:
                pass

        def visible():
            out = []
            for ch in lst.GetChildren():
                try:
                    cn = ch.ClassName or ""
                    nm = ch.Name or ""
                    if cn == "mmui::ChatItemView":  # 时间分隔行跳过
                        continue
                    if predicate(cn, nm):
                        out.append((cn, nm, ch))
                except Exception:
                    continue
            return out

        # 列表定位在底部(最新)。match_last 直接取当前可视区最匹配的一条
        # （最新消息已实例化），没有才向上(滚向历史)翻找。
        if match_last:
            got = visible()
            if got:
                ch = got[-1][2]
                try:
                    r = ch.BoundingRectangle
                    return (got[-1][0], got[-1][1], r)
                except Exception:
                    pass
            for _ in range(max_scrolls):
                got = visible()
                if got:
                    ch = got[-1][2]
                    try:
                        r = ch.BoundingRectangle
                        return (got[-1][0], got[-1][1], r)
                    except Exception:
                        pass
                scroll('down', 2)  # 滚向历史找更早匹配
            return None

        # 非 match_last：从底部(最新)向历史逐屏扫描
        seen = set()
        for _ in range(max_scrolls):
            for cn, nm, ch in visible():
                try:
                    r = ch.BoundingRectangle
                    key = (r.left, r.top, r.right, r.bottom)
                except Exception:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                if predicate(cn, nm):
                    return (cn, nm, r)
            scroll('down', 2)
        return None

    # ------------------------------------------------------------------ 调试
    def dump(self, max_depth: int = 16, max_nodes: int = 1500):
        """打印主窗口 UIA 树（控件名失效时重新勘察）。"""
        if not self.ensure_window():
            print("UIA 树不可用")
            return
        win = self._win
        cnt = [0]

        def walk(c, d=0):
            if d > max_depth or cnt[0] > max_nodes:
                return
            for k in c.GetChildren():
                if cnt[0] > max_nodes:
                    break
                cnt[0] += 1
                try:
                    print("  " * d + f"{k.ControlTypeName} class={k.ClassName!r} "
                          f"name={(k.Name or '')[:30]!r} aid={k.AutomationId!r}")
                except Exception:
                    continue
                walk(k, d + 1)

        walk(win)
