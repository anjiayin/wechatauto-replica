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
from wechatauto.text_norm import normalize_match_text, contains_normalized

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
# 「引用最新一条」读消息列表的重试参数：窗口刚从托盘/最小化恢复、或 UIA 树
# 刚重绑时消息列表会读空，一次读空就判失败会白丢一次发送（2026-11-28 实测
# 「@ + 引用」1.3s 快速失败、8 分钟后同参数成功）。重试最多 4×0.4s≈1.6s，
# 仍读不到照旧返回 None——不降级成「不引用直接发」。
_LATEST_ROW_ATTEMPTS = 4
_LATEST_ROW_WAIT = 0.4
# Qt 内部消息泵窗口：枚举顶层窗口时跳过，其 UIA 服务可能阻塞调用
QT_INTERNAL_WIN_CLASS_PREFIX = "QEventDispatcherWin32_Internal_"
# 聊天标题栏里的「企业名」标签：**只有企业微信/客服会话才有**这个控件。
# 打开会话后靠它判断「当前这个会话是不是企业微信身份的那一个」——同名
# 好友与企业微信联系人（如「安佳胤」与「安佳胤@我的车车」）在搜索框里
# 名字完全一样，只看标题无法区分，误开就会把消息发给错的人。
#
# 微信 4.1.x（2026-11-13 本机实测）把整个标题区从 ``top_content_h_view``
# 搬到了 ``content_view.top_content_view.title_h_view``：控件名
# ``current_chat_openim_name`` 还在，但父链全变了。只认旧路径的后果是
# **所有企业微信联系人都发不出去**——校验永远返回「不是企业微信会话」，
# 日志表现为「同名会话身份不符：xx 打开后不是企业微信会话，已放弃」。
# 因此这里存多套路径逐个试，顺序 = 新版本优先。
OPENIM_NAME_AIDS = (
    # 微信 4.1.x 起（本机实测，安佳胤@我的车车 可命中）
    "content_view.top_content_view.title_h_view.left_v_view"
    ".left_content_v_view.left_ui_.openim_name_view.current_chat_openim_name",
    # 微信 4.0.x 旧布局（保留兼容）
    "top_content_h_view.top_spacing_v_view.top_left_info_v_view"
    ".big_title_line_h_view.current_chat_openim_name",
)
# 兼容旧引用（历史上按单个常量使用过）
OPENIM_NAME_AID = OPENIM_NAME_AIDS[0]
# 标题区容器 AID（新旧版本都在）：取不到说明控件树还没物化
TITLE_VIEW_AID = "content_view.top_content_view.title_h_view"
# 控件树是否已物化的判定锚点：微信 4.x 主视图（Qt 外壳阶段只有 2 个子控件，
# 此时任何 EditControl/TextControl 查找都会超时，表现为「会话打不开」）
MAIN_VIEW_AID = "MainView"
# 搜索结果里企业微信联系人的分区名（微信 4.x 单列一个分区）
OPENIM_SECTIONS = {"企业微信联系人", "企业微信"}
DEFAULT_EXE = r"C:\Program Files\Tencent\Weixin\Weixin.exe"

# 搜索结果分区标题（aid 为空且名字命中此集合的才算分区头，其余空 aid 视为建议项）
SECTION_HEADERS = {
    "最常使用", "最近使用", "联系人", "群聊", "公众号", "服务号", "订阅号",
    "聊天记录", "收藏", "功能", "小程序", "最近使用过的小程序", "视频号",
    "企业微信联系人", "朋友圈", "搜索网络结果", "企业微信",
}

# 主窗口顶栏导航（聊天/通讯录/收藏/朋友圈...）：微信 4.x 为
# mmui::MainTabBar，第一个按钮即「聊天」页。用户停在收藏/通讯录/发现等
# 页面时，左栏没有全局搜索框或搜索只在当前页面内生效，发送链路会一直
# 「未搜到目标」；发送前必须先切回聊天页（2026-11-28 实机日志证实）。
CHAT_TAB_BAR_CLASS = "mmui::MainTabBar"
CHAT_TAB_BAR_AIDS = ("main_tabbar", "MainView.main_tabbar")
CHAT_TAB_NAMES = ("聊天", "微信", "Chats", "Weixin", "WeChat")
# 聊天页标志：会话列表容器。存在即说明主窗口停在聊天页。
CHAT_MASTER_CLASS = "mmui::ChatMasterView"
# 微信自身的图片/视频预览浮窗（朋友圈/聊天里点开大图）。类名含
# PreviewWindow；与主窗口同进程，会盖在搜索框上方截走物理点击。
PREVIEW_WIN_CLASS_KEYWORD = "PreviewWindow"

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

    def __init__(self, timeout: float = 15.0, search_timeout: float = 2.0,
                 hwnd: int = None, pid: int = None):
        self.timeout = timeout
        # 双开定向：只枚举/操作目标账号实例的窗口，绝不唤醒或新开实例
        # （weixin:// 会拉起默认账号，回复就会落到另一个微信上）。
        self._target_hwnd = int(hwnd or 0)
        self._target_pid = int(pid or 0)
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
        # 服务一启动就把用户级环境变量落盘（幂等）：客户机用户随时可能
        # 手动重启微信，晚了就来不及继承（进程环境只在启动时读取）。
        try:
            self._ensure_user_env()
        except Exception:
            pass

    # ------------------------------------------------------------------ 基础
    @staticmethod
    def is_running() -> bool:
        try:
            import subprocess
            out = subprocess.run(["tasklist", "/fi", "imagename eq Weixin.exe",
                                  "/nh"], capture_output=True, text=True,
                                 timeout=10,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                 ).stdout or ""
            return "Weixin.exe" in out
        except Exception:
            return False

    def wake(self) -> None:
        """weixin:// 协议唤起/显示窗口（托盘态也能拉起）；失败则拉起 exe。

        拉起前先确保 ``QT_ACCESSIBILITY=1``：微信 4.1.15 起只改内存里的
        accessibility 开关字节已经无法让 UIA 树物化（字节写入成功但 Qt 不再
        重建 Provider），必须**在 Qt 初始化之前**就带上这个环境变量——由
        我们拉起的微信进程会继承它，树从启动起就是完整的，不再依赖版本
        特征扫描（微信每次更新都不用重新适配）。用户自己双击启动的微信
        不继承进程环境变量，靠用户级环境变量（见 :meth:`_ensure_user_env`）
        或内存补丁兜底。
        """
        self._ensure_qt_accessibility()
        # 用户级环境变量（注册表）：客户机上用户手动双击启动的微信继承的是
        # Explorer 的环境块，只写进程环境覆盖不到——必须在注册表里落一份。
        self._ensure_user_env()
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

    @staticmethod
    def _ensure_qt_accessibility() -> None:
        """确保本进程环境里 ``QT_ACCESSIBILITY=1``（子进程会继承）。"""
        if os.environ.get("QT_ACCESSIBILITY", "").strip() == "1":
            return
        os.environ["QT_ACCESSIBILITY"] = "1"
        wxlog.info("已设置 QT_ACCESSIBILITY=1（微信 UIA 树随启动完整暴露，"
                   "不再依赖内存补丁）")

    @staticmethod
    def _ensure_user_env() -> bool:
        """把 ``QT_ACCESSIBILITY=1`` 写入当前用户环境（HKCU\\Environment）。

        仅写进程环境（:meth:`_ensure_qt_accessibility`）只能覆盖由我们拉起
        的微信；部署到客户机后，用户自己双击启动的微信继承的是 Explorer 的
        环境块——必须把变量落到注册表并广播 ``WM_SETTINGCHANGE``，Explorer
        立即刷新后，此后手动启动的微信也带上该变量。**客户机无需任何手工
        配置**。幂等：已为 1 时不重写、不广播。

        Returns:
            True = 变量已就绪（本次写入或本就存在）；False = 写注册表失败。
        """
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                                winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
                try:
                    cur, _ = winreg.QueryValueEx(key, "QT_ACCESSIBILITY")
                except OSError:
                    cur = None
                if cur is not None and str(cur).strip() == "1":
                    return True
                winreg.SetValueEx(key, "QT_ACCESSIBILITY", 0,
                                  winreg.REG_SZ, "1")
        except Exception as e:
            wxlog.warning("写入用户级环境变量 QT_ACCESSIBILITY 失败：%s", e)
            return False
        WeChatUIA._broadcast_env_change()
        wxlog.info("已写入用户级环境变量 QT_ACCESSIBILITY=1"
                   "（用户手动启动微信也生效）")
        return True

    @staticmethod
    def _broadcast_env_change() -> None:
        """广播 ``WM_SETTINGCHANGE``，让 Explorer 立即刷新环境块。

        只写注册表的话，Explorer 要等重新登录才感知——期间用户手动启动的
        微信仍拿不到变量。广播后新启动的进程立即继承。
        """
        try:
            HWND_BROADCAST, WM_SETTINGCHANGE = 0xFFFF, 0x001A
            SMTO_ABORTIFHUNG = 0x0002
            res = wintypes.DWORD()
            ctypes.windll.user32.SendMessageTimeoutW(
                HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
                SMTO_ABORTIFHUNG, 2000, ctypes.byref(res))
        except Exception:
            pass

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

    def _hot_activate_accessibility(self, hwnd: int, rearm: bool = False):
        """运行中热激活 Qt accessibility gate，不重启微信进程。

        Returns:
            ``'patched'`` 真实写了 0→1（只有这种情况才算一次补丁）；
            ``'already'`` gate 本来就是 1，什么都没写；
            ``False``    读写失败 / 版本不支持。

        旧实现把 ``'already'`` 也返回 True，调用方照单计数、清 UIA 缓存，
        于是「什么都没做」也吃掉限流配额：实测日志只出现 3 次 0→1，
        限流却已报「已热激活 5 次」，随后 300 秒内全部强制降级 OCR，
        用户输入框定位失败、白等 60~80 秒（时好时坏的直接来源）。

        ``rearm=True`` 处理「gate 已经是 1、但控件树被 Qt 回收」的死锁：
        微信关闭到托盘/最小化再恢复时，Qt 会销毁 UIA 树却把 gate 留在 1，
        此时普通热激活读到 1 直接返回 ``already``，什么都不做 → 干等 30 秒
        超时（2026-11-15 09:54 日志「真实写入补丁 0 次」）。这里做一次
        **0→1 脉冲**强制 Qt 重新物化树。脉冲写内存次数计入限流配额，调用方
        必须严格限次（见 :meth:`_wake_and_wait_ready`）。
        """
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
                if not rearm:
                    return 'already'
                # 0→1 脉冲：让 Qt 认为 accessibility 被重新打开并重建树。
                # 严格限次由调用方保证，避免「90 秒内 4+ 次补丁写挂 Qt 输入」。
                if not self._write_process_byte(handle, address, 0):
                    wxlog.warning("重触发 UIA 树失败：无法写入 gate=0。")
                    return False
                time.sleep(0.05)
                if not self._write_process_byte(handle, address, 1):
                    wxlog.warning("重触发 UIA 树失败：无法写回 gate=1。")
                    return False
                wxlog.info("已重触发微信 UIA 树（gate 1→0→1）：PID=%s", pid)
                return 'patched'
            if current is None:
                wxlog.warning("热激活 UIA 失败：无法读取 active byte。")
                return False
            if not self._write_process_byte(handle, address, 1):
                wxlog.warning("热激活 UIA 失败：无法写入 active byte。")
                return False
            wxlog.info("已热激活微信 UIA 树：PID=%s Weixin.dll+0x%x: %s -> 1",
                       pid, rva, current)
            return 'patched'
        finally:
            kernel32.CloseHandle(handle)

    # 热激活限流（类级，跨实例共享）：微信 4.1.15 实测在短时间内对同一
    # 进程反复写 accessibility gate byte（0->1->0->1 循环）会把 Qt 输入
    # 系统弄挂——窗口渲染正常、WM_NULL 响应、CPU 空闲，但所有鼠标点击
    # （包括 PostMessage 直投）都被吞掉，只能重启微信恢复（2026-11-14
    # 实测：90 秒内补丁 4+ 次 → 假死）。限流后发送链路 fail-closed 直接
    # 判失败（2026-11-15 起不再降级截图/OCR）。
    PATCH_COOLDOWN = 20.0        # 距上次热激活的最小间隔（秒）
    PATCH_MAX_PER_PID = 5        # 同一微信进程代内最多热激活次数
    MAX_WRITES_IN_WAIT = 2       # 单次等待循环内最多真实写内存次数
    REARM_COOLDOWN = 45.0        # 同一 PID 两次「gate 0→1 重触发」最小间隔
    _patch_log: dict = {}        # pid -> [count, last_monotonic]
    _rearm_log: dict = {}        # pid -> last_monotonic（重触发单独限流）
    _throttled_pid: int = 0      # 最近被限流拒绝的 PID（供上层给可操作提示）
    _throttled_at: float = 0.0   # 该次拒绝的 monotonic 时刻（5 分钟内有效）

    def _restore_window_visible(self) -> bool:
        """把**真正的 Win32 最小化**主窗口恢复为可见。

        微信 4.1 的 Qt accessibility 树只在窗口可见时物化。但「关闭到
        托盘」不是 Win32 最小化，而是 Qt 自己 hide()
        （``IsWindowVisible=False``）：2026-11-15 11:33:44 实测，用
        ``SW_SHOW`` 强行显示后 Qt 内部仍是托盘态 → 树死活不物化
        （16s + 31s 两次唤醒超时），窗口还卡成 visible=True+iconic=True
        的 -32000 伪矩形（用户看到「界面不能动」）；唤醒流程反复写
        gate byte 又把 Qt 输入系统写挂（同 PID 6 次写入后配额打满）。

        因此这里只处理 ``iconic=True`` 的真最小化（SW_RESTORE 实测有效）；
        托盘态**绝不碰窗口**，返回 False 并置 ``_tray_hidden_detected``，
        让所有唤醒路径直接 fail-closed，一个字节都不写。

        Returns:
            True  = 至少一个主窗口可见且非最小化，可以安全热激活；
            False = 托盘隐藏 / 窗口不存在 / 枚举失败，调用方不得唤醒。
        """
        self._tray_hidden_detected = False
        if not _HAS_WIN32:
            return True                 # 非 Windows（CI）不做窗口操作
        restored = 0
        visible_ok = False
        tray_hidden = False

        def cb(h, _):
            nonlocal restored, visible_ok, tray_hidden
            try:
                if win32gui.GetWindowText(h) not in MAIN_NAMES:
                    return True
                if not self._is_target_hwnd(h):
                    return True       # 双开：其它账号的窗口状态与我无关
                visible = bool(win32gui.IsWindowVisible(h))
                iconic = bool(win32gui.IsIconic(h))
                if not visible:
                    # Qt hide() = 关到托盘：SW_SHOW 只会制造畸形态，不碰
                    tray_hidden = True
                    return True
                if iconic:
                    win32gui.ShowWindow(h, win32con.SW_RESTORE)
                    restored += 1
                visible_ok = visible_ok or (
                    bool(win32gui.IsWindowVisible(h))
                    and not bool(win32gui.IsIconic(h)))
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            return False
        if restored:
            wxlog.info('微信主窗口被最小化，已恢复可见（%d 个）', restored)
        if tray_hidden:
            self._tray_hidden_detected = True
            wxlog.warning('微信主窗口关闭到系统托盘（Qt 隐藏），'
                          '跳过窗口恢复与热激活：请点开微信主窗口后重试')
        return visible_ok and not tray_hidden

    def _wake_accessibility(self, rearm: bool = False) -> bool:
        """按 PID 去重地热激活；**只有真实写内存**才消耗限流配额。

        两个实测坑：
        1) 同一微信进程可能有多个标题为「微信」的顶层窗口，旧实现逐个
           打补丁、逐个计数，一次调用就能吃掉好几份配额；
        2) gate byte 本来就是 1 时「什么都没写」也算一次成功热激活，
           还顺手清掉 ``_win`` 缓存，让 UIA 树锚点反复重建。

        本方法**只管补丁**，不改窗口状态：窗口可见性由
        :meth:`_wake_and_wait_ready` / :meth:`ensure_window` 在调用前恢复，
        不可见时它们会直接跳过补丁（不可见状态写 gate byte 既不会物化
        控件树，反复写还会挂掉 Qt 输入系统）。
        """
        ok = False
        patched_any = False
        seen_pids = set()
        self._last_patch_written = False
        for hwnd in self._wechat_hwnds():
            pid = self._pid_from_hwnd(hwnd)
            if pid in seen_pids:
                continue          # 同进程只处理一次，避免一次调用连写多遍
            seen_pids.add(pid)
            now = time.monotonic()
            rec = WeChatUIA._patch_log.setdefault(pid or 0, [0, 0.0])
            if now - rec[1] < WeChatUIA.PATCH_COOLDOWN:
                wxlog.debug("距上次热激活不足 %.0fs，跳过（PID=%s）",
                            WeChatUIA.PATCH_COOLDOWN, pid)
                continue
            if rec[0] >= WeChatUIA.PATCH_MAX_PER_PID:
                if now - rec[1] > 300:
                    # 长时间无补丁后再现 → 可能已换代，重置计数
                    WeChatUIA._patch_log[pid or 0] = [0, 0.0]
                    rec = WeChatUIA._patch_log[pid or 0]
                else:
                    WeChatUIA._throttled_pid = pid or 0
                    WeChatUIA._throttled_at = time.monotonic()
                    wxlog.warning(
                        "微信 UIA 已热激活 %d 次仍反复回收（PID=%s），"
                        "疑似微信异常，本次跳过（发送将直接判失败）。建议重启微信。",
                        rec[0], pid)
                    continue
            # rearm=False 时走旧的单参数调用形式，保持与既有调用方/测试桩
            # 兼容；只有真正需要 0→1 脉冲时才传新参数。
            state = (self._hot_activate_accessibility(hwnd, rearm=True)
                     if rearm else self._hot_activate_accessibility(hwnd))
            if state == 'already':
                ok = True        # gate 已开：不写内存、不占配额、不清缓存
                continue
            if not state:
                continue
            ok = True
            patched_any = True
            WeChatUIA._throttled_pid = 0
            rec[0] += 1
            rec[1] = time.monotonic()
        if patched_any:
            self._win = None
            time.sleep(0.2)
        self._last_patch_written = patched_any
        return ok

    # ------------------------------------------------------------------ 窗口定位
    def _is_target_hwnd(self, hwnd, pid: Optional[int] = None) -> bool:
        """窗口是否属于当前绑定的微信实例（未绑定时不做限制）。"""
        target_hwnd = getattr(self, '_target_hwnd', 0)
        target_pid = getattr(self, '_target_pid', 0)
        if target_hwnd and hwnd == target_hwnd:
            return True
        if target_pid:
            if pid is None:
                pid = self._pid_from_hwnd(hwnd)
            return bool(pid) and pid == target_pid
        if target_hwnd:
            return False
        return True

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
                        if not self._is_target_hwnd(h, pid):
                            return True   # 双开：忽略其它账号实例的窗口
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
        # 绑定目标实例时绝不能 wake()：weixin:// / Weixin.exe 只会拉起
        # 默认账号，可能新开一个别的微信实例，后续点击全会落到它上面。
        instance_bound = bool(getattr(self, '_target_pid', 0)
                              or getattr(self, '_target_hwnd', 0))
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
            if (not instance_bound and self._login_window() is None
                    and time.time() - last_wake > 6):
                self.wake()
                last_wake = time.time()
            time.sleep(0.8)
        raise UIATimeout("等待微信主窗口超时（客户端未就绪）。")

    def ensure_window(self, wake: bool = True, timeout: Optional[float] = None,
                      require_ready: bool = True) -> bool:
        """确保可访问的主窗口存在且控件树可用，并置前。

        优先 ControlFromHandle 按句柄锚定拿 mmui 树；若树未物化（只剩 Qt
        外壳：2 个子控件、所有查找都超时）则对当前 Weixin 进程热激活并等
        它物化，不重启微信。任一环节失败返回 False，由调用方降级到 OCR。

        Args:
            require_ready: 默认 True。**必须校验控件树可用**——只看到窗口
                就返回 True 会让上层以为「窗口在」，随后 search_box /
                chat_input 全部返回 None，最后报成「无法定位会话」，
                用户看到的却是微信明明已经打开。实测未物化时首次要等
                15~30 秒，这段等待应当显式发生并打日志。
        """
        timeout = timeout or self.timeout
        w = self._find_main()
        if w is not None:
            self._win = w
            if not require_ready or self.is_ready():
                self._activate(w)
                return True
            # 窗口在但控件树没物化：等它（不重启微信）。发送路径不再强制
            # 等满 30s：那是冷启动首次物化的耗时，已由启动预热承担。
            # 窗口从托盘恢复后 tree 重触发通常 1~3s 内就绪，等 30s 只会
            # 让用户白等（2026-11-15 用户反馈「太慢」）。
            if wake and self._wake_and_wait_ready(min(max(timeout, 12.0), 15.0)):
                if self._win is not None:
                    self._activate(self._win)
                return True
            self._activate(w)
            return False
        if not wake:
            return False
        try:
            # 托盘/最小化窗口在 _wechat_hwnds() 里根本枚举不到；先恢复可见
            # 再判断，否则会误走「微信没开」的 screen reader / wake 分支。
            self._restore_window_visible()
            if getattr(self, '_tray_hidden_detected', False):
                # 托盘态下这条分支的 _wake_accessibility()/wake() 也会写
                # gate byte；必须整体跳过，否则后台巡检会把 Qt 输入写挂。
                wxlog.warning('微信在托盘，ensure_window 跳过唤醒分支（不写补丁）')
                return False
            self._last_patch_written = False
            if self._login_window() is not None:
                self._auto_login()
            elif self._wechat_hwnds():
                self._wake_accessibility()
            elif getattr(self, '_target_pid', 0) or getattr(self, '_target_hwnd', 0):
                # 目标账号实例没有窗口：可能已退出/切号。禁止唤醒或新开
                # 实例（会拉起默认账号，导致回复到另一个微信）。
                wxlog.warning('当前监控账号的微信窗口不存在（可能已退出或'
                              '切换了账号），不唤醒/不新开微信实例，本次操作失败')
                return False
            else:
                self._set_screen_reader_flag(True)
                self.wake()
            # 本窗口内是否真写过 gate byte（_wait_main 内部的补丁也算）
            patched = bool(getattr(self, '_last_patch_written', False))
            self._wait_main(timeout)
            patched = patched or bool(
                getattr(self, '_last_patch_written', False))
            if self._win is None:
                return False
            if not require_ready or self.is_ready():
                return True
            # 只有刚写过 gate byte 时，Qt 才可能正在后台重建 MainView：
            # 实测根窗口出现后子控件还要 0.2~5s（2026-11-28 18:20 打包版
            # 日志：补丁成功 2s 后找 MainTabBar 仍为空，3s 后本次发送被判
            # tree_not_ready，而下一轮补丁后同一页面 4s 就切页成功）。
            # 根窗口类名出现 ≠ 控件树可用，这里补等物化再放行，最多 10s；
            # 没写补丁（gate 已 1/限流）时干等无益，立即按现状返回。
            if patched:
                deadline = time.time() + 10.0
                while time.time() < deadline:
                    if self.is_ready():
                        return True
                    time.sleep(0.25)
            return self.is_ready()
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

    # ------------------------------------------------------------ 页面/遮挡归一
    def _chat_page_visible(self, win) -> bool:
        """主窗口当前是否停在「聊天」页（会话列表页）。"""
        if win is None:
            return False
        try:
            return bool(win.GroupControl(
                ClassName=CHAT_MASTER_CLASS).Exists(0.5, 0.1))
        except Exception:
            return False

    def _chat_tab_button(self, win):
        """定位顶栏的「聊天」按钮；找不到返回 None。"""
        tabbar = None
        for find in (lambda: win.ToolBarControl(ClassName=CHAT_TAB_BAR_CLASS),
                     lambda: win.ToolBarControl(
                         AutomationId=CHAT_TAB_BAR_AIDS[0]),
                     lambda: win.ToolBarControl(
                         AutomationId=CHAT_TAB_BAR_AIDS[1])):
            try:
                tb = find()
                if tb.Exists(0.3, 0.1):
                    tabbar = tb
                    break
            except Exception:
                continue
        if tabbar is None:
            return None
        for nm in CHAT_TAB_NAMES:
            try:
                b = tabbar.ButtonControl(Name=nm)
                if b.Exists(0.2, 0.1):
                    return b
            except Exception:
                continue
        # 非中英文语言包兜底：聊天页永远是顶栏第一个按钮
        try:
            for c in tabbar.GetChildren():
                if (c.ControlTypeName or "") == "ButtonControl":
                    return c
        except Exception:
            pass
        return None

    def _ensure_chat_tab(self, win=None, force: bool = False) -> bool:
        """把主窗口切回「聊天」页；返回切换后是否处于聊天页。

        收藏/通讯录/发现/朋友圈等页面没有全局搜索框（或搜索只在当前页
        内生效），`open_chat` 的搜索必然「未搜到目标」。优先 Invoke（不
        依赖鼠标，窗口被部分遮挡也有效），不支持时退回物理点击。
        """
        win = win or self._win
        if win is None:
            return False
        if not force and self._chat_page_visible(win):
            return True
        btn = self._chat_tab_button(win)
        if btn is None:
            wxlog.debug("切回聊天页失败：未找到 MainTabBar 聊天按钮")
            return False
        for how in ("invoke", "click"):
            try:
                if how == "invoke":
                    pattern = btn.GetInvokePattern()
                    if pattern is None:
                        continue
                    pattern.Invoke()
                else:
                    btn.Click()
            except Exception:
                continue
            time.sleep(0.5)
            if self._chat_page_visible(win):
                wxlog.info("已把微信主窗口切回「聊天」页（%s）", how)
                return True
        return False

    def _dismiss_preview_windows(self) -> int:
        """关闭微信自身的图片/视频预览浮窗（朋友圈点开大图会盖住主窗口）。

        预览窗与主窗口同属一个微信进程，``_minimize_blockers`` 只处理其它
        进程的遮挡窗口（微信自身窗口不能最小化，Qt 会回收控件树）。预览窗
        在上方时物理点击全部落到它身上，搜索框拿到空内容 → 「未搜到目标」。
        这里只对类名含 PreviewWindow 的顶层窗口投递 WM_CLOSE（等价于点
        右上角关闭大图），不动主窗口、不写 gate byte。
        """
        if not _HAS_WIN32 or self._win is None:
            return 0
        try:
            pid = self._win.ProcessId
        except Exception:
            return 0
        if not pid:
            return 0
        targets = []

        def cb(h, _):
            try:
                if not win32gui.IsWindowVisible(h):
                    return True
                cls = win32gui.GetClassName(h) or ""
                if PREVIEW_WIN_CLASS_KEYWORD not in cls:
                    return True
                if self._pid_from_hwnd(h) != pid:
                    return True
                targets.append(h)
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            return 0
        for h in targets:
            try:
                win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
        if targets:
            wxlog.info("已关闭微信图片/视频预览窗 %d 个（发送前清理遮挡）",
                       len(targets))
            time.sleep(0.3)
        return len(targets)

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

    @staticmethod
    def _is_openim_result(r: dict, hint: str = "") -> bool:
        """搜索结果条目是否企业微信/客服身份。

        三个证据（任一命中即认）：
        1. 分区标题为企业微信联系人（微信 4.x 单列一个分区）；
        2. 副标题含「企业微信」标签；
        3. 副标题含企业名（调用方从显示名「昵称@企业名」里带过来）。
        副标题在微信里可能只渲染企业名（没有「企业微信」字样），所以第 3
        条是必要的，否则同名好友与企业微信会同时被判为「都不是」。
        """
        sub = str(r.get("sub") or "")
        if (r.get("section") or "") in OPENIM_SECTIONS:
            return True
        if "企业微信" in sub:
            return True
        return bool(hint) and hint in sub

    def _collect_results(self, keyword: str, settle: float = 1.5,
                         prefer_openim: bool = False,
                         openim_hint: str = "",
                         keep_freq: bool = False) -> List[dict]:
        """采集去重后的候选 [{name, section, aid, cell}]。

        Args:
            prefer_openim: 目标确定是企业微信联系人时置 True：在候选里优先
                保留企业微信身份那一条，避免同名好友抢占。
            openim_hint: 企业简称（从「昵称@企业名」里拆出来），用于在
                副标题里认出企业微信那条。
            keep_freq: 保留「最常使用/最近使用」分区里与普通分区 aid 重复的
                条目。**企业微信同名联系人只能靠这里救回来**：实测搜「安佳胤」
                得到『最常使用/安佳胤』（企业微信，标题带 @我的车车）与
                『联系人/安佳胤』（普通好友）两条，两者的 name 与
                AutomationId 完全相同，旧的「aid 去重」会把『最常使用』那条
                删掉，于是永远只剩普通好友 → 发送被拒或发错人。
        """
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
        # 企业微信身份优先（必须在「精确同名」筛选之后、去重之前做）：
        # 同名好友与企业微信联系人两条同名结果，精确筛选会两条都留下；
        # 若企业微信那条在搜索里显示成「昵称@企业名」，精确筛选只留下
        # 普通好友，此时必须回到全量候选里把企业微信那条找回来。
        if prefer_openim:
            pool = matched if len(matched) > 1 else raw
            wx = [r for r in pool if self._is_openim_result(r, openim_hint)]
            if wx:
                matched = wx

        # 最常使用/最近使用中重复出现的同一 aid 去重
        if keep_freq:
            pruned = list(matched)
        else:
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

    def _search_in_box(self, box, keywords: List[str], retries: int,
                       prefer_openim: bool,
                       openim_hint: str) -> Tuple[List[dict], str]:
        """逐关键词在搜索框里搜索并采集候选；返回 (results, used_kw)。"""
        for kw in keywords:
            for _ in range(max(1, retries)):
                self._paste_into(box, kw, clear=True)
                time.sleep(0.8)
                got = self._collect_results(
                    kw, prefer_openim=prefer_openim,
                    openim_hint=openim_hint,
                    keep_freq=prefer_openim)
                if got:
                    return got, kw
                time.sleep(0.4)
        return [], (keywords[-1] if keywords else "")

    def open_chat(self, keyword: str, index: Optional[int] = None,
                  section: Optional[str] = None, retries: int = 2,
                  prefer_openim: bool = False,
                  openim_hint: str = "") -> bool:
        """搜索并打开联系人/群聊，成功后校验输入框 Name。返回是否成功。

        传入 username/wxid 时自动映射为昵称/备注/微信号再搜索（微信搜索框
        不认 wxid）。搜索框残留会影响命中，每次打开前先清空重试。

        Args:
            prefer_openim: 目标确定是企业微信/客服联系人时置 True。同名好友
                与企业微信身份在搜索结果里名字完全一样（如「安佳胤」与
                「安佳胤@我的车车」），默认取第一条会开成普通好友，消息就
                发给了错的人；置 True 后优先选企业微信分区/带「企业微信」
                副标题的那一条。
            openim_hint: 企业简称（「昵称@企业名」里 @ 后面那段），副标题
                只渲染企业名时靠它认出企业微信那条。
        """
        if not self.ensure_window():
            return False
        win = self._win
        # 朋友圈点开大图时预览窗盖住主窗口：物理点击全部落空，先清遮挡。
        self._dismiss_preview_windows()
        # 非聊天页（收藏/通讯录/发现…）没有全局搜索框或只在页内搜索，
        # 先切回聊天页，保证接下来的搜索走全局联系人搜索。
        self._ensure_chat_tab(win)
        box = self._search_box(win)
        if box is None:
            # 切页后搜索框可能还没物化：强制再归一一次并重找。
            self._ensure_chat_tab(win, force=True)
            box = self._search_box(win)
        if box is None:
            return False

        keywords = self._resolve_search_keyword(keyword)
        results, used_kw = self._search_in_box(
            box, keywords, retries, prefer_openim, openim_hint)
        if not results:
            # 空结果兜底：搜索框可能属于其它页面的局部搜索（例如收藏页
            # 只搜收藏内容），切回聊天页清空重搜一轮再判失败。
            self._ensure_chat_tab(win, force=True)
            box = self._search_box(win)
            if box is not None:
                results, used_kw = self._search_in_box(
                    box, keywords, retries, prefer_openim, openim_hint)

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
        # 搜索结果里只靠名称无法区分；此时优先企业微信分区/副标题标注
        # 「企业微信」的那一条。副标题缺失或都不命中时保持原有取第一条行为。
        # 触发条件有两类：① 调用方直接传了 @openim/@kefu 账号；② 调用方
        # 明确告知 prefer_openim（发送链路用显示名搜索时走这条）。
        low_kw = (keyword or "").strip().lower()
        want_openim = bool(prefer_openim) or low_kw.endswith("@openim") \
            or "@kefu" in low_kw
        if len(results) > 1 and want_openim:
            wx = [r for r in results if self._is_openim_result(r, openim_hint)]
            if wx:
                results = wx

        chosen = results[0]
        if want_openim and openim_hint:
            # 同名多条时**只有点开才能分辨**：实测搜「安佳胤」得到
            # 『最常使用/安佳胤』（企业微信，标题带 @我的车车）与
            # 『联系人/安佳胤』（普通好友）两条，name 与 AutomationId 完全
            # 相同；靠分区名或副标题都会判错（一条都没标「企业微信」）。
            # 因此逐个点开读「@企业名」标签，读到匹配的那条才算命中。
            if self._open_matching_openim(results, openim_hint):
                return True
            return False
        chosen["cell"].Click()
        time.sleep(0.7)

        name = self.current_chat()
        # same_chat 容忍群名「(123)」后缀与空格差异：严格相等会把
        # 「搜索点击已成功」误判为失败，导致调用方落入十几秒的 OCR 兜底
        if name and (self.same_chat(name, chosen["name"])
                     or self.same_chat(name, used_kw)):
            return True
        return False

    def _open_matching_openim(self, results: List[dict], corp: str) -> bool:
        """逐个候选点开，用标题栏的「@企业名」标签确认是不是企业微信那条。

        只在「目标确定是企业微信联系人」且知道企业简称时使用。逐条点开的
        代价是每条约 0.6~1.0 秒，但这是唯一可靠的判别方式——同名普通好友与
        企业微信联系人在搜索结果里的 name / AutomationId 完全相同。

        排序上优先「最常使用/最近使用」：企业微信会话通常是刚处理过的那个，
        多半一次就中，实测总耗时 1~2 秒。

        点开只切换会话，**不输入、不发送**，即使点错了也不会误发消息。
        """
        ordered = sorted(
            results,
            key=lambda r: 0 if r["section"] in ("最常使用", "最近使用") else 1)
        for r in ordered:
            try:
                r["cell"].Click()
            except Exception:
                continue
            time.sleep(0.6)
            label = self.openim_label()
            if label and self._corp_in_label(label, corp):
                wxlog.debug("企业微信身份已确认：%s 标签=%s（候选 %s）",
                            r["name"], label, r["section"] or "-")
                return True
        return False

    @staticmethod
    def _corp_in_label(label: str, corp: str) -> bool:
        """「@企业名」标签是否指向目标企业（容忍后缀/空格差异）。"""
        lab = str(label or "").lstrip("@").strip()
        c = str(corp or "").strip()
        if not lab or not c:
            return False
        return lab == c or c in lab or lab in c

    def fill_text(self, text: str) -> bool:
        """把文字填进输入框但**不发送**（人工确认模式下用）。返回是否成功。

        与 :meth:`send_text` 的唯一区别是不按 Enter：消息留在输入框里，
        由用户在微信窗口里自己核对、自己点发送。发送动作不可撤回，误发到
        同名联系人的代价远大于多一次人工点击，所以「先填后确认」必须是
        独立动作，不能靠调用方及时打断。
        """
        if not self.ensure_window():
            return False
        e = self._chat_input()
        if e is None:
            return False
        self._paste_into(e, text, clear=True)
        time.sleep(0.2)
        # 回读输入框内容确认真的填进去了（粘贴偶发失败时要报错，否则用户
        # 以为文字已在输入框里等着，实际空的）。注意：输入框的 .Name 是
        # 「当前会话名 + 语音输入提示语」而不是正文，只能走 ValuePattern；
        # 该控件不支持取内容时按成功处理（不能因读不到就判失败）。
        head = (text.strip().splitlines() or [""])[0][:12]
        try:
            value = e.GetValuePattern().Value
        except Exception:
            return True
        return (not head) or (head in str(value or ""))

    def openim_label(self) -> Optional[str]:
        """当前会话标题栏的企业名标签文本（如 ``'@我的车车'``）。

        这是「这条会话到底是不是企业微信身份」的唯一可靠依据：同名好友与
        企业微信联系人的标题、输入框 Name 都是同一个昵称，只有这里多一个
        ``@企业名`` 标签。

        Returns:
            ``'@企业名'`` = 确认是企业微信/客服会话；
            ``''`` = 标题区存在但**没有**该标签（同名普通好友/群）；
            ``None`` = 探测失败（控件树未物化），调用方不应据此拦截发送。
        """
        if not self.ensure_window():
            return None
        win = self._win
        if win is None:
            return None
        # ① 按已知 AID 直取（新版本在前，旧版本在后）
        for aid in OPENIM_NAME_AIDS:
            try:
                ctrl = win.TextControl(AutomationId=aid)
                if ctrl.Exists(0, 0):
                    return (ctrl.Name or "").strip() or "@"
            except Exception:
                continue
        # ② AID 又变了也能认出来：标题区子树里找「以 @ 开头的文本」。
        #    微信改父链是常事（4.0→4.1 就整段搬家），但「企业名带 @ 前缀」
        #    是产品文案，稳定性远高于控件路径。
        try:
            title = win.GroupControl(AutomationId=TITLE_VIEW_AID)
            if not title.Exists(0.5, 0.1):
                # 连标题区都拿不到 = 控件树没物化，不能断言「不是企业微信」
                return None
            return self._find_at_label(title, depth=6) or ""
        except Exception:
            return None

    @staticmethod
    def _find_at_label(ctl, depth: int = 6) -> str:
        """在控件子树里找第一个以 ``'@'`` 开头的文本（企业名标签）。"""
        if depth <= 0:
            return ""
        try:
            kids = ctl.GetChildren()
        except Exception:
            return ""
        for k in kids:
            try:
                name = (k.Name or "").strip()
            except Exception:
                name = ""
            if name.startswith("@"):
                return name
        for k in kids:
            got = WeChatUIA._find_at_label(k, depth - 1)
            if got:
                return got
        return ""

    def is_openim_chat(self) -> Optional[bool]:
        """当前打开的会话是否企业微信/客服身份。

        Returns:
            True = 有企业名标签（企业微信/客服会话）；False = 明确没有该
            标签（普通好友/群）；None = 探测失败（控件树不可用），调用方
            不应据此拦截发送。
        """
        label = self.openim_label()
        if label is None:
            return None
        return bool(label)

    def is_ready(self) -> bool:
        """控件树是否已物化（微信把 UIA 树放出来了）。

        未物化时 ``_find_main`` 仍能拿到 Qt 外壳窗口（只有 2 个子控件），
        随后任何 ``EditControl``/``TextControl`` 查找都会超时，表现为
        「搜索框找不到 → 无法定位会话」。这里用主视图 MainView 判断，
        与 ``_chat_input`` 走同一套查找机制，命中时约几十毫秒。
        """
        win = self._win
        if win is None:
            return False
        try:
            return bool(win.GroupControl(
                AutomationId=MAIN_VIEW_AID).Exists(0.4, 0.1))
        except Exception:
            return False

    def warm_tree(self, timeout: float = 30.0,
                  allow_rearm: bool = True) -> bool:
        """预热控件树：只要求可访问性树就绪，**不激活窗口、不动前后台**。

        微信 4.x 的 UIA 树在被系统/微信自己回收后要重新物化，实测首次
        需要 15~30 秒。这段时间如果压在用户点「发送」上，用户只会看到
        「微信到前台后半天没动静」。放到服务启动的后台线程里做掉，用户
        第一次发送就只剩搜索+点击的几秒。

        注意：树未物化时窗口类名是 Qt 外壳类而非 ``mmui::MainWindow``，
        ``_find_main()`` 必然返回 None——**不能**据此判定微信没开而直接
        失败，必须走 ``_wake_and_wait_ready``（热激活补丁 + 等待物化）。
        否则预热在冷树场景下永远空转（2026-11-14 实测踩坑：预热日志一直
        「未完成」，首次发送仍然要付出完整物化耗时）。

        Args:
            allow_rearm: 后台预热巡检传 False，禁止 1→0→1 重触发。预热是
                无人值守轮询，而重触发每次写 2 个 gate byte 并计入 5 次/
                300s 限流配额——2026-11-28 18:06~18:13 打包版日志里巡检
                在用户尚未发送时就连做 5 次重触发，把配额烧光后所有发送
                直接判失败。发送路径仍保留重触发机会。
        """
        w = self._find_main()
        if w is not None:
            self._win = w
            if self.is_ready():
                return True
        return self._wake_and_wait_ready(timeout, allow_rearm=allow_rearm)

    def _wake_and_wait_ready(self, timeout: float = 30.0,
                             allow_rearm: bool = True) -> bool:
        """热激活并等待控件树就绪（不主动置前，但必须保证窗口可见）。

        树物化的前提是窗口**可见**：用户把微信关到托盘/最小化后，光打补丁
        永远等不到树。这里在每次重试前恢复可见性，并限制单次等待内真实写
        内存的次数（反复写会把 Qt 输入系统写挂，实测 90s 内 4+ 次即假死）。
        """
        t0 = time.time()
        wxlog.info("微信控件树尚未就绪，正在热激活（首次约 15~30 秒，请稍等）")
        visible = self._restore_window_visible()
        if not visible:
            # 窗口仍不可见：控件树在不可见窗口上不会物化，打补丁也无效。
            # **立刻失败、一个字节都不写**，不要干等满 30 秒。
            if getattr(self, '_tray_hidden_detected', False):
                wxlog.warning("微信已关闭到系统托盘（Qt 隐藏）：控件树不会"
                              "物化，本次不写任何补丁、立即判失败；"
                              "请点开微信主窗口后重试")
            else:
                wxlog.warning("微信主窗口不可见（最小化且恢复失败），"
                              "控件树不会物化，本次立即放弃")
            return False
        writes = 0
        self._wake_accessibility()
        if getattr(self, '_last_patch_written', False):
            writes += 1
        # 关键：'already'（gate 本来就是 1）不算写入。窗口刚从托盘恢复时
        # Qt 把树回收了却把 gate 留在 1，普通热激活什么都不做 → 干等超时
        # （09:54 日志「真实写入补丁 0 次」）。此时做一次受控 0→1 重触发。
        rearm_done = False
        deadline = t0 + max(timeout, 12.0)
        next_wake = t0 + 3.0
        missing = 0
        while time.time() < deadline:
            w = self._find_main()
            if w is None:
                # 微信被关掉/退回登录态：连续几次确认后再放弃，避免窗口
                # 标题瞬时变化被误判成「微信消失」。
                if not self._wechat_hwnds():
                    missing += 1
                    if missing >= 3:
                        wxlog.warning("等待控件树期间微信窗口消失，放弃本次预热")
                        return False
                else:
                    missing = 0
            else:
                missing = 0
                self._win = w
                if self.is_ready():
                    wxlog.info("微信控件树已就绪（耗时 %.1fs）",
                               time.time() - t0)
                    return True
            if time.time() >= next_wake:
                # 用户可能在这 6 秒里又把微信关回托盘：先恢复可见性。
                still_visible = self._restore_window_visible()
                if not still_visible:
                    wxlog.warning("等待控件树期间微信窗口又变为不可见，立即放弃")
                    return False
                elapsed = time.time() - t0
                if (allow_rearm and not rearm_done and writes == 0
                        and elapsed >= 2.5 and self._rearm_allowed()):
                    # gate=1 但树没物化：做唯一一次 0→1 脉冲强制重建。
                    self._wake_accessibility(rearm=True)
                    rearm_done = True
                    if getattr(self, '_last_patch_written', False):
                        # 一次 1→0→1 脉冲实际写 **2 个字节**；按 1 次计数
                        # 会让 MAX_WRITES_IN_WAIT 形同虚设（11:33:49 实测
                        # 一次 rearm + 后续普通补丁 = 90s 内 4+ 次写入）。
                        writes += 2
                        wxlog.info("已对「gate=1 但树未物化」做一次受控重触发")
                elif writes < (WeChatUIA.MAX_WRITES_IN_WAIT if allow_rearm
                               else 1):
                    # 后台巡检（allow_rearm=False）最多补 1 个字节：它是无人
                    # 值守轮询，写多了既没用又烧配额/伤 Qt 输入。
                    self._wake_accessibility()
                    if getattr(self, '_last_patch_written', False):
                        writes += 1
                next_wake = time.time() + 6.0
            time.sleep(0.4)
        wxlog.warning(
            "微信控件树唤醒超时（已等 %.0fs，本次真实写入补丁 %d 次，"
            "重触发 %s）。常见原因：①微信被关闭到托盘/最小化（控件树不会"
            "物化，请点开微信窗口）；②4.1.15+ 需要「QT_ACCESSIBILITY=1 "
            "环境 + 热激活补丁」齐备，手动先于服务启动的微信需重启一次"
            "继承环境变量。",
            time.time() - t0, writes, "已尝试" if rearm_done else "未触发")
        return False

    def _rearm_allowed(self) -> bool:
        """同一 PID 的 0→1 重触发限流：45 秒内最多一次。

        已知风险：短时间内反复写 accessibility gate 会把 Qt 输入系统写挂
        （窗口渲染正常但鼠标点击全被吞）。重触发是额外的写内存动作，必须
        比普通补丁更严格地限流。
        """
        pids = {self._pid_from_hwnd(h) for h in self._wechat_hwnds()}
        now = time.monotonic()
        for pid in pids:
            if pid and now - WeChatUIA._rearm_log.get(pid, 0.0) < WeChatUIA.REARM_COOLDOWN:
                wxlog.debug("距上次 UIA 重触发不足 %.0fs，跳过（PID=%s）",
                            WeChatUIA.REARM_COOLDOWN, pid)
                return False
            # 距**任何一次真实补丁写入**也必须满 45s。否则「刚 0→1 完
            # 20 秒又来一次 1→0→1」在同一进程里连写 3 个字节，叠加后续
            # 唤醒就踩中「90s 内 4+ 次写挂 Qt 输入」（11:31~11:34 实测）。
            rec = WeChatUIA._patch_log.get(pid or 0)
            if rec and rec[0] and now - rec[1] < WeChatUIA.REARM_COOLDOWN:
                wxlog.debug("距上次真实补丁不足 %.0fs，跳过重触发（PID=%s）",
                            WeChatUIA.REARM_COOLDOWN, pid)
                return False
        for pid in pids:
            if pid:
                WeChatUIA._rearm_log[pid] = now
        return True

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
    def _right_click_row(self, row) -> Optional[Tuple[int, int]]:
        """在消息行内容重心处右键，返回右键坐标；失败返回 None。

        ``row`` 既可以是 UIA 控件，也可以是 ``BoundingRectangle``/RECT
        （``find_in_message_list`` 返回的第三项就是矩形）。旧实现只认控件，
        滚动命中后传矩形进来会 AttributeError 被 except 吞掉 → 引用失败
        降级 OCR；且旧实现返回 bool，菜单没弹出时 ``_set_cursor(*pos)``
        又会因 bool 不可解包而抛异常。这里统一返回坐标供上层重试。
        """
        try:
            r = getattr(row, "BoundingRectangle", row)
            if r.right - r.left < 10 or r.bottom - r.top < 10:
                return None
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
            return (cx, cy)
        except Exception as exc:
            wxlog.debug(f"右键消息行失败：{exc}")
            return None

    def _find_row_by_text(self, target_text: str) -> Optional[Tuple[int, int]]:
        """按文本定位消息行并右键，返回右键坐标；失败返回 None。

        两处关键修正（2026-11-15 实测）：
        1. 比较前做 Unicode 空白归一化——微信 @ 消息里的 U+2005 被
           ``_quote_snippet`` 折叠成普通空格，旧代码拿它直接 ``in`` UIA
           Name，永远匹配不上；
        2. 找不到时 ``find_in_message_list`` 会把列表滚回最新位置，避免
           聊天面板停在历史消息处污染后续输入/右键（旧代码最多滚 12 次
           且不复位，用户看到的就是「滑块一直在动却不输入」）。
        找到时**不**复位：此刻右键菜单已弹出，滚轮会把菜单关掉；发送后
        微信自身会跳回最新消息。
        """
        text = normalize_match_text(target_text)
        if not text:
            return None
        lst = self._message_list()
        if lst is None:
            return None
        # 先只在可视区找：绝大多数引用目标就是最近几条。右键失败时不要
        # 直接放弃，继续走滚动查找（截图/坐标抖动导致的单次失败可自愈）。
        for ch in list(lst.GetChildren()):
            try:
                if contains_normalized(text, ch.Name):
                    pos = self._right_click_row(ch)
                    if pos:
                        return pos
            except Exception:
                continue
        got = self.find_in_message_list(
            lambda cn, nm: contains_normalized(text, nm), max_scrolls=12)
        return self._right_click_row(got[2]) if got else None

    def _menu_click(self, keywords: Tuple[str, ...]) -> bool:
        """在当前弹出的右键菜单里点击名称含任一关键词的菜单项。"""
        for kw in keywords:
            item = self._uia_find_menu_item(kw)
            if item is not None and self._uia_click_menu_item(item):
                return True
        return False

    def _wechat_popup_windows(self) -> list:
        """微信当前可见的浮层窗口（右键菜单等独立顶层窗口，不含主窗口）。

        2026-11-28 实测根因：微信 4.x 的消息右键菜单不在主窗口 UIA 子树里，
        而是独立顶层弹窗（旧版 ``ui/component.py`` 的 ``Menu`` 就是按
        ``mmui::XMenu`` 顶层窗口查找的）。主窗口树里永远没有「引用」节点，
        1.4.4/1.4.5 所有引用发送都卡在「没找到「引用」菜单项」——撤回的
        UIA 路径同样从未命中，一直被 OCR 兜底掩盖。

        只枚举微信 PID 自己的可见顶层窗口，不遍历桌面 UIA 根树（避免
        UIA 根遍历的系统挂起 bug）。
        """
        main_hwnd = 0
        pid = 0
        try:
            main_hwnd = int(self._win.NativeWindowHandle or 0)
            pid = int(self._win.ProcessId or 0)
        except Exception:
            main_hwnd = int(getattr(self, "_target_hwnd", 0) or 0)
            pid = int(getattr(self, "_target_pid", 0) or 0)
        popups = []
        if _HAS_WIN32 and pid:
            hwnds = []

            def _collect(hwnd, _extra):
                try:
                    if hwnd == main_hwnd:
                        return True
                    if not win32gui.IsWindowVisible(hwnd):
                        return True
                    if win32process.GetWindowThreadProcessId(hwnd)[1] != pid:
                        return True
                    cls = win32gui.GetClassName(hwnd)
                    if cls.startswith(QT_INTERNAL_WIN_CLASS_PREFIX):
                        return True
                    hwnds.append(hwnd)
                except Exception:
                    pass
                return True

            try:
                win32gui.EnumWindows(_collect, None)
            except Exception:
                hwnds = []
            for hwnd in hwnds:
                try:
                    ctrl = auto.ControlFromHandle(hwnd)
                    if ctrl is not None:
                        popups.append(ctrl)
                except Exception:
                    continue
            return popups
        # 无 win32 环境（极端兜底）：桌面直连子窗口里按 PID 过滤
        try:
            for w in auto.GetRootControl().GetChildren():
                try:
                    if pid and w.ProcessId != pid:
                        continue
                    if main_hwnd and w.NativeWindowHandle == main_hwnd:
                        continue
                    if not w.IsOffscreen:
                        popups.append(w)
                except Exception:
                    continue
        except Exception:
            pass
        return popups

    @staticmethod
    def _menu_item_nodes(popup, max_depth: int = 5) -> list:
        """浮层窗口里的菜单项控件（``MenuItemControl`` / ``mmui::XMenuView``）。"""
        items = []

        def walk(c, d=0):
            if d > max_depth:
                return
            try:
                children = c.GetChildren()
            except Exception:
                return
            for ch in children:
                try:
                    cn = ch.ClassName or ""
                    ctype = ch.ControlTypeName or ""
                    if cn == "mmui::XMenuView" or ctype == "MenuItemControl":
                        items.append(ch)
                except Exception:
                    pass
                walk(ch, d + 1)

        walk(popup)
        return items

    def _ocr_click_menu_item(self, keywords: Tuple[str, ...]) -> bool:
        """浮层菜单里按 OCR 文字点击菜单项（UIA 未暴露菜单项时的兜底）。

        只在**确认微信浮层窗口已打开**后调用：截图限定在各浮层窗口矩形
        内，只点击 OCR 命中关键词的那一项；找不到直接失败，绝不盲点。
        """
        popups = self._wechat_popup_windows()
        if not popups:
            return False
        left = top = None
        right = bottom = None
        for p in popups:
            try:
                r = p.BoundingRectangle
            except Exception:
                continue
            if r.right - r.left < 8 or r.bottom - r.top < 8:
                continue
            left = r.left if left is None else min(left, r.left)
            top = r.top if top is None else min(top, r.top)
            right = r.right if right is None else max(right, r.right)
            bottom = r.bottom if bottom is None else max(bottom, r.bottom)
        if left is None:
            return False
        try:
            from wechatauto.guia import ScreenOCR
            import PIL.ImageGrab as IG
        except Exception:
            return False
        pad = 4
        bbox = (left - pad, top - pad, right + pad, bottom + pad)
        try:
            img = IG.grab(bbox=bbox)
        except Exception:
            return False
        try:
            res = ScreenOCR.recognize(img)
        except Exception:
            return False
        wanted = tuple(k.lower() for k in keywords)
        for text, x, y, w, h in res:
            t = (text or "").replace(" ", "").lower()
            if not t:
                continue
            if any(k in t for k in wanted):
                cx = bbox[0] + x + w // 2
                cy = bbox[1] + y + h // 2
                self._set_cursor(cx, cy)
                time.sleep(0.2)
                self._left_click()
                time.sleep(0.3)
                return True
        return False

    def _click_menu_item(self, keywords: Tuple[str, ...],
                         retry_pos: Optional[Tuple[int, int]] = None) -> bool:
        """点击右键菜单项（主窗口树 → 浮层窗口 → 有界 OCR）。

        ``retry_pos``：菜单完全没弹出时补一次右键的坐标。菜单已经打开时
        **绝不补右键**——菜单就弹在右键坐标处，再右键会点到菜单项里。
        """
        if self._menu_click(keywords):
            return True
        if self._context_menu_open():
            return self._ocr_click_menu_item(keywords)
        if retry_pos is None:
            return False
        self._set_cursor(*retry_pos)
        time.sleep(0.2)
        self._right_click()
        time.sleep(0.8)
        if self._menu_click(keywords):
            return True
        if self._context_menu_open():
            return self._ocr_click_menu_item(keywords)
        return False

    @staticmethod
    def _key_esc() -> None:
        """按一次 Esc（关右键菜单）；单独抽出便于测试打桩，不发真实按键。"""
        try:                                   # VK_ESCAPE / KEYEVENTF_KEYUP
            ctypes.windll.user32.keybd_event(0x1B, 0, 0, 0)
            ctypes.windll.user32.keybd_event(0x1B, 0, 2, 0)
        except Exception:
            pass

    def _menu_popup_windows(self) -> list:
        """确认是右键菜单的浮层窗口（UIA 类名含 Menu 或含菜单项节点）。"""
        menus = []
        for popup in self._wechat_popup_windows():
            try:
                cn = popup.ClassName or ""
            except Exception:
                cn = ""
            if "Menu" in cn:
                menus.append(popup)
            elif self._menu_item_nodes(popup):
                menus.append(popup)
        return menus

    def _context_menu_open(self) -> bool:
        """右键菜单是否处于打开状态（浮层窗口或主窗口树内的菜单节点）。

        微信 4.x 的消息右键菜单是**独立顶层浮层窗口**（``mmui::XMenu``），
        不在主窗口子树里；单看 ``_uia_find_menu_item("")`` 会漏判。
        """
        if self._wechat_popup_windows():
            return True
        return self._uia_find_menu_item("") is not None

    def _close_context_menu(self) -> None:
        """关闭右键菜单（不发 Esc，避免误隐藏微信主窗口）。

        微信主窗口在「没有菜单」时收到 Esc 会隐藏到系统托盘（2026-11-28
        实测：私聊引用失败后窗口被藏、界面持续报「微信窗口不可用」）。
        这里改为点一下聊天输入框——点击菜单外的任意位置即可关闭菜单，
        而输入框本来就是下一步要操作的控件，最坏情况也无副作用。只有
        确认是菜单浮层（``mmui::XMenu``）且输入框不可用时才退回 Esc。
        """
        if not self._context_menu_open():
            return
        try:
            e = self._chat_input()
        except Exception:
            e = None
        if e is not None:
            try:
                r = e.BoundingRectangle
                self._set_cursor((r.left + r.right) // 2, r.bottom - 12)
                time.sleep(0.12)
                self._left_click()
                time.sleep(0.2)
            except Exception:
                pass
            return
        if self._menu_popup_windows():
            self._key_esc()
            time.sleep(0.2)

    def dismiss_quote_menu(self) -> None:
        """UIA 引用失败后的现场清理（**不再降级 OCR**）。

        旧流程在「菜单没弹出 / 菜单项没识别到」时直接降级 OCR，此时右键
        菜单可能仍开着、消息列表可能停在历史消息处，实测会连续「未检测到
        输入框」、右键点到历史消息、找不到「引用」菜单项，白等 20~30 秒。
        现在发送链路 fail-closed 直接判失败，这里只关掉**确实还开着**的
        菜单并滚回最新，给下一次操作留一个干净的起点。
        """
        self._close_context_menu()
        try:
            lst = self._message_list()
            if lst is None:
                return
            r = lst.BoundingRectangle
            self._set_cursor((r.left + r.right) // 2,
                             (r.top + r.bottom) // 2)
            for _ in range(16):               # 兜底滚回最新（滚到底自然停）
                self._mouse_wheel(-120)
                time.sleep(0.02)
            time.sleep(0.2)
        except Exception as exc:
            wxlog.debug(f"引用降级前清理失败（忽略）：{exc}")

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

    def _prepare_quote(self, target_text: Optional[str] = None):
        """建立「引用卡片」并把光标放回输入框末尾，返回输入框控件。

        顺序是硬约束：清草稿必须在右键引用**之前**（此时还没有引用卡片），
        引用之后绝不能再 Ctrl+A/Delete，否则引用块会被一起删掉，发出去的
        就变成普通消息。抽出这一步是为了让「引用 + @」组合复用同一条路径。

        ``target_text=None`` 表示引用「最新一条**对方**消息」：微信不支持
        引用自己发送的消息（右键菜单没有「引用」项），私聊里自己刚回复过
        时，右键字面最新一条必然失败（2026-11-28 实测：私聊引用失败后
        窗口还被 Esc 清理误隐藏）。
        """
        self._last_quote_error = ""
        e0 = self._chat_input()
        if e0 is not None:
            self._clear_input(e0)
        if target_text:
            # _find_row_by_text 内部完成「归一化匹配 + 右键」，返回坐标
            pos = self._find_row_by_text(target_text)
            if pos is None:
                wxlog.debug("引用目标未在消息列表中定位到：%s",
                            (target_text or "")[:30])
                self._last_quote_error = "未在消息列表中定位到要引用的消息"
                return None
        else:
            pos = self._right_click_latest_row(None, incoming_only=True)
        if not pos:
            self._last_quote_error = (
                "没有可引用的对方消息（最新一条可能是您自己发送的，"
                "微信不支持引用自己的消息）")
            return None
        if not self._click_menu_item(("引用", "Quote"), retry_pos=pos):
            wxlog.debug("引用失败：右键消息行后没有找到「引用」菜单项"
                        "（浮层窗口 %d 个）",
                        len(self._wechat_popup_windows()))
            self._close_context_menu()
            self._last_quote_error = (
                "右键菜单里没有「引用」项（该消息可能不支持引用，"
                "或最新一条不是对方发送的）")
            return None
        time.sleep(0.5)
        e = self._chat_input()
        if e is None:
            self._last_quote_error = "引用卡片已建立，但未找到输入框"
            return None
        # 点击菜单后焦点可能落在消息区：先把光标放回输入框末尾
        self._focus_input_end(e)
        return e

    def last_quote_error(self) -> str:
        """最近一次引用准备/发送的失败原因（空串 = 没有错误）。"""
        return getattr(self, "_last_quote_error", "")

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
                **对方发送**的消息（微信不支持引用自己的消息）
        """
        self._last_quote_error = ""
        if not self.ensure_window():
            self._last_quote_error = "微信窗口不可用"
            return False
        if who and not self.same_chat(self.current_chat(), who):
            if not self.open_chat(who):
                self._last_quote_error = f"无法打开会话「{who}」"
                return False
            time.sleep(0.5)
        e = self._prepare_quote(target_text)
        if e is None:
            return False
        # 草稿里已经有「引用卡片」，这里绝不能再 Ctrl+A/Delete
        if not self._paste_append(e, text):
            self._last_quote_error = "引用卡片已建立，但正文写入输入框失败"
            return False
        time.sleep(0.2)
        try:
            e.SendKeys("{Enter}", waitTime=0.05)
        except Exception:
            self._last_quote_error = "引用消息已填充，但发送按键失败"
            return False
        return True

    # -- @ 群成员 -------------------------------------------------------
    def _mention_popup(self, timeout: float = 2.0):
        """定位 @ 成员弹层（mmui::XPopover / AutomationId=MentionPopover）。

        微信 4.x 的 Qt 弹层**不一定是 WindowControl**：同一份代码里右键菜单
        ``mmui::XMenuView`` 就只能靠遍历控件树按 ClassName 命中（见
        :meth:`_uia_find_menu_item`）。旧实现只试 ``WindowControl(...)``，
        弹层以 Pane/Custom 形态暴露时三个变体全部落空，两次调用各等满
        timeout（日志：「@ 成员未在弹层中选中」+ 输入发送约 4.9s）。
        这里保留原快路径，再补「不绑定 ControlType 的通用搜索」和
        「主窗口子树按 ClassName/AutomationId 遍历」两条兜底；都找不到
        仍返回 None 由调用方 fail-closed，绝不降级截图/OCR。
        """
        deadline = time.time() + timeout
        win = self._win
        if win is None:
            return None
        window_variants = (
            dict(ClassName="mmui::XPopover", AutomationId="MentionPopover"),
            dict(AutomationId="MentionPopover"),
            dict(ClassName="mmui::XPopover", Name="Weixin"),
        )
        generic_variants = (
            dict(ClassName="mmui::XPopover", AutomationId="MentionPopover"),
            dict(AutomationId="MentionPopover"),
        )
        while time.time() < deadline:
            # 1) 原快路径：只匹配 ControlType.Window 的弹层
            for kw in window_variants:
                try:
                    pop = win.WindowControl(searchDepth=0xFFFFFFFF, **kw)
                    if pop.Exists(0.1, 0.1):
                        return pop
                except Exception:
                    continue
            # 2) 通用控件搜索：覆盖 Pane/Custom 形态的 Qt 弹层
            for kw in generic_variants:
                try:
                    pop = win.Control(searchDepth=0xFFFFFFFF, **kw)
                    if pop.Exists(0.1, 0.1):
                        return pop
                except Exception:
                    continue
            # 3) 主窗口子树遍历：只按 ClassName/AutomationId 认弹层
            found = self._find_mention_popover_in_tree(win)
            if found is not None:
                return found
            time.sleep(0.2)
        return None

    @staticmethod
    def _find_mention_popover_in_tree(root, max_depth: int = 8):
        """在主窗口子树内按 ClassName/AutomationId 找 @ 弹层（不限控件类型）。

        与 :meth:`_uia_find_menu_item` 同思路：Qt 自绘弹层的 UIA ControlType
        不可靠，ClassName / AutomationId 才是稳定锚点。限深避免整树遍历。
        """
        def walk(ctrl: object, depth: int = 0):
            if depth > max_depth:
                return None
            try:
                children = ctrl.GetChildren()
            except Exception:
                return None
            for child in children:
                try:
                    cn = child.ClassName or ""
                    aid = child.AutomationId or ""
                    nm = child.Name or ""
                except Exception:
                    cn = aid = nm = ""
                if aid == "MentionPopover" or (
                        cn == "mmui::XPopover" and nm == "Weixin"):
                    return child
                hit = walk(child, depth + 1)
                if hit is not None:
                    return hit
            return None

        try:
            return walk(root)
        except Exception:
            return None

    @staticmethod
    def _match_mention_item(items, target: str,
                            allow_single: bool = False):
        """按「归一化精确 → 归一化包含 →（受限）唯一项」匹配成员控件。

        两级调用语义（``allow_single`` 由调用方按弹层可信度传入）：

        * **未过滤**（``allow_single=False``，默认）：弹层是推荐/全量列表，
          此时「唯一项」毫无可信度——名字与目标完全不符也会选中，等于
          @ 错人。因此禁用唯一项兜底，匹配不到就返回 ``None``（fail-closed）。
        * **已过滤**（``allow_single=True``）：调用方已把目标名字敲进搜索框，
          弹层被过滤到唯一项是可信的，可作为兜底命中；但仍必须通过
          「最低字符关联」守卫：归一化后 target 与该唯一项 Name 至少共享
          一个非空白字符，否则视为「完全不符」同样拒绝。

        共享字符只取 :mod:`wechatauto.text_norm` 归一化后字符串的字符集合
        求交，不做全半角/繁简转换——后者会引入误匹配，与本模块既有约定
        一致。``target`` 在入口再归一化一次，保证直接调用也自洽（幂等）。
        """
        target = normalize_match_text(target)
        hit = next((i for i in items
                    if normalize_match_text(getattr(i, "Name", "")) == target),
                   None)
        if hit is None:
            hit = next((i for i in items
                        if contains_normalized(target,
                                               getattr(i, "Name", ""))), None)
        if hit is None and allow_single and len(items) == 1:
            # 已过滤场景的唯一项兜底：仍需最低字符关联守卫，
            # 完全无共享字符（=完全不符）时拒绝，绝不猜一个 @ 上去。
            name_chars = {c for c in normalize_match_text(
                getattr(items[0], "Name", "")) if not c.isspace()}
            target_chars = {c for c in target if not c.isspace()}
            if target_chars and name_chars and target_chars & name_chars:
                hit = items[0]
        return hit

    def _pick_mention(self, member: str, timeout: float = 2.0,
                      allow_single: bool = False) -> bool:
        """在已弹出的 @ 成员列表里选中 member（精确名 → 归一化包含 → 唯一项）。

        旧实现只读 ``pop.ListControl().GetChildren()`` 的直接子节点，且用裸
        ``str.strip()`` 比较；弹层成员项被包在更深的容器里、或 Name 带 U+2005
        等空白时就一个都匹配不上。这里补一层「弹层内按名字做后代
        ListItemControl 搜索」（与 :class:`wechatauto.ui.chatbox.AtMenu` 同款），
        并统一用 :mod:`wechatauto.text_norm` 归一化，消除空白差异。

        ``allow_single`` 直接透传给 :meth:`_match_mention_item`：未过滤时
        保持 ``False`` 以禁用无条件唯一项兜底；已按名字过滤时才传 ``True``，
        且仍受「最低字符关联」守卫约束。后代 ``ListItemControl(Name=probe)``
        是精确匹配，本身安全，不受该开关影响。
        """
        pop = self._mention_popup(timeout)
        if pop is None:
            wxlog.debug("@ 弹层未定位到（WindowControl/通用控件/树遍历均未命中）")
            return False
        target = normalize_match_text(member)
        if not target:
            return False
        items = []
        try:
            lst = pop.ListControl()
            if lst.Exists(0.2, 0.1):
                items = [i for i in lst.GetChildren()
                         if normalize_match_text(getattr(i, "Name", ""))]
        except Exception as exc:
            wxlog.debug("读取 @ 成员列表失败：%s", exc)
        hit = self._match_mention_item(items, target,
                                       allow_single=allow_single)
        # 后代按名字精确搜索：覆盖成员项不在 ListControl 直接子节点的情况
        if hit is None:
            for probe in (member, target):
                try:
                    cand = pop.ListItemControl(Name=probe)
                    if cand.Exists(0.2, 0.1):
                        hit = cand
                        break
                except Exception:
                    continue
        if hit is None:
            wxlog.debug("@ 弹层成员项与「%s」均不匹配（已读 %d 项）",
                        member, len(items))
            return False
        try:
            auto.RollIntoView(pop, hit)
        except Exception:
            pass
        try:
            hit.Click()
        except Exception as exc:
            wxlog.debug("点击 @ 成员项「%s」失败：%s", member, exc)
            return False
        time.sleep(0.25)
        return True

    def at_members(self, members: List[str], text: str,
                   who: Optional[str] = None,
                   quote: bool = False,
                   quote_target_text: Optional[str] = None) -> bool:
        """群聊 @ 多个成员后一次性发送正文（同一条消息）。

        旧实现是「每个成员各发一条消息」，@ 多个成员时会发出多条空正文
        消息；这里改成：逐个 @ 进同一个草稿 → 追加正文 → 只回车一次。

        ``quote=True`` 时先建立引用卡片，再在同一个草稿里 @ ——微信允许
        引用与 @ 共存。引用准备由 :meth:`_prepare_quote` 完成，它内部会
        在**引用之前**清草稿；引用之后不能清空，否则引用卡片会被删掉。
        ``quote_target_text=None`` 表示引用最新一条**对方**消息（微信不
        支持引用自己的消息）。
        """
        names = [m.strip() for m in (members or []) if m and m.strip()]
        if not names:
            return (self.quote_message(text, who=who,
                                       target_text=quote_target_text)
                    if quote else self.send_text(text))
        if not self.ensure_window():
            return False
        if who and not self.same_chat(self.current_chat(), who):
            if not self.open_chat(who):
                return False
            time.sleep(0.5)
        if quote:
            e = self._prepare_quote(quote_target_text)
            if e is None:
                return False
        else:
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
            # 第一次：弹层未过滤（推荐/全量列表），唯一项兜底必须禁用，
            # 否则名字完全不符时也会选中 → @ 错人。
            if not self._pick_mention(name):
                wxlog.debug(f"@ 成员未在弹层中选中：{name}")
                # 退一步：输入名字过滤后再选（部分版本 @ 后不弹全量列表）
                try:
                    e.SendKeys(name, waitTime=0.05)
                except Exception:
                    return False
                time.sleep(0.6)
                # 第二次：弹层已按名字过滤，唯一项可信，但仍受最低字符
                # 关联守卫约束，完全不符时依旧 fail-closed。
                if not self._pick_mention(name, timeout=1.5,
                                          allow_single=True):
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

    def _row_content_center_x(self, r) -> Optional[int]:
        """消息行内非背景内容的水平重心（绝对坐标；截图失败返回 None）。

        微信消息行 rect 是全宽：对方消息内容靠左、自己发送的靠右。方向
        判定与右键落点共用本方法（方向不能按半个屏幕估算，必须在行内
        实测，否则自己气泡偏右时点击会落到气泡外）。
        """
        try:
            from PIL import ImageGrab as IG
            import numpy as np
            img = IG.grab(bbox=(r.left, r.top, r.right, r.bottom))
            arr = np.array(img.convert("RGB"))
            bg = (arr.max(axis=2) > 235) & ((arr.max(axis=2) - arr.min(axis=2)) < 22)
            colc = (~bg).sum(axis=0)
            total = int(colc.sum())
            if total < 20:
                return None
            return int(r.left + sum(x * colc[x] for x in range(len(colc))) / total)
        except Exception:
            return None

    def _latest_message_row(self, incoming_only: bool = False):
        """取可视区最底部（最新）的消息行 (控件, 矩形)；读不到返回 None。

        时间分隔行（ChatItemView）与系统提示行不算消息；高度 <40px 的行是
        尚未布局完的占位，右键会落到空白处，一并跳过。

        ``incoming_only=True`` 时只返回对方发送的行（内容靠左）：微信
        不支持引用自己发送的消息，私聊里「引用最新一条」在自己刚回复过
        时右键到自己的气泡，菜单没有「引用」必然失败（2026-11-28 实测）。
        """
        lst = self._message_list()
        if lst is None:
            return None
        candidates = []
        for ch in list(lst.GetChildren()):
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
        candidates.sort(key=lambda t: t[1].bottom, reverse=True)
        if not incoming_only:
            return candidates[0]
        for ch, r in candidates:
            cx = self._row_content_center_x(r)
            if cx is None:
                continue
            if cx < (r.left + r.right) // 2:
                return (ch, r)
        wxlog.debug("可视区内没有对方发送的消息行（最新是自发消息），"
                    "无法建立引用")
        return None

    def _right_click_latest_row(self, who: Optional[str] = None,
                                incoming_only: bool = False) -> Optional[Tuple[int, int]]:
        """打开会话并右键最新一条消息行，返回气泡内右键坐标 (x, y)；失败返回 None。

        ``incoming_only=True``：跳过自己发送的消息（微信不支持引用自己的
        消息），供引用回复使用；撤回自己的消息仍用默认的 False。
        """
        if not self.ensure_window():
            return None
        if who and not self.current_chat() == who:
            if not self.open_chat(who):
                return None
        # 窗口刚从托盘/最小化恢复、或 UIA 树刚重绑时，消息列表会读空（树还没
        # 物化），一次读空就判失败会让整条「引用 / 引用+@」发送白丢一次。重试
        # 上限 4 次 × 0.4s ≈ 1.6s，仍读不到照旧返回 None（fail-closed 不变，
        # 绝不降级成「不引用直接发」）。
        target = None
        for attempt in range(_LATEST_ROW_ATTEMPTS):
            target = self._latest_message_row(incoming_only=incoming_only)
            if target is not None:
                break
            if attempt < _LATEST_ROW_ATTEMPTS - 1:
                time.sleep(_LATEST_ROW_WAIT)
        if target is None:
            wxlog.debug("引用最新一条失败：连续 %d 次没读到可引用的消息行"
                        "（窗口可能刚恢复、控件树未就绪，或最新是自发消息）",
                        _LATEST_ROW_ATTEMPTS)
            return None
        ch, r = target
        # 消息行内取内容重心 x（self 靠右、friend 靠左），y 取行垂直中心
        cx = self._row_content_center_x(r)
        if cx is None:
            cx = (r.left + r.right) // 2
        cy = (r.top + r.bottom) // 2
        self._set_cursor(cx, cy)
        time.sleep(0.2)
        self._right_click()
        time.sleep(1.0)
        return (cx, cy)

    def _uia_find_menu_item(self, name_sub: str, max_depth: int = 6):
        """查找右键菜单项控件：先主窗口树，再微信浮层窗口。

        微信 4.x 的消息右键菜单是独立顶层浮层窗口（``mmui::XMenu`` /
        ``MenuItemControl``，见 ``_wechat_popup_windows``），主窗口树里
        根本没有菜单节点；只扫主窗口会导致「引用」永远点不到（撤回的
        UIA 路径同样从未命中，一直靠 OCR 兜底掩盖）。两处都要求控件
        可见（``IsOffscreen`` 为 False），避免命中关闭后的残留节点。
        返回匹配的控件或 None。
        """

        def visible(c) -> bool:
            try:
                return not c.IsOffscreen
            except Exception:
                return True

        w = getattr(self, "_win", None)
        if w is not None:
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
                    if cn == "mmui::XMenuView" and name_sub in nm and visible(ch):
                        found[0] = ch
                        return
                    walk(ch, d + 1)

            try:
                walk(w)
            except Exception:
                pass
            if found[0] is not None:
                return found[0]
        popups = self._wechat_popup_windows()
        for popup in popups:
            for ch in self._menu_item_nodes(popup):
                try:
                    if name_sub in (ch.Name or "") and visible(ch):
                        return ch
                except Exception:
                    continue
        if popups:
            # 菜单确实在浮层里但 UIA 没暴露可匹配的菜单项 → 交给 OCR 兜底
            return None
        return self._deep_menu_item(name_sub)

    def _deep_menu_item(self, name_sub: str, max_depth: int = 12):
        """主窗口深层子树里的菜单项（菜单嵌在聊天视图里的版本）。

        聊天视图本身就在主窗口第 7 层开外，浅层遍历（``max_depth=6``）
        根本到不了；用 ``searchFromControl`` 限定在主窗口内做一次定向
        搜索，命中后要求控件可见，避免命中关闭后的残留节点。
        """
        w = getattr(self, "_win", None)
        if w is None:
            return None
        kwargs = dict(searchFromControl=w, searchDepth=max_depth,
                      ClassName="mmui::XMenuView")
        if name_sub:
            kwargs["RegexName"] = f".*{re.escape(name_sub)}.*"
        try:
            ctrl = auto.Control(**kwargs)
            if ctrl is None or not ctrl.Exists(0, 0):
                return None
            try:
                if ctrl.IsOffscreen:
                    return None
            except Exception:
                pass
            return ctrl
        except Exception:
            return None

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
                             max_scrolls: int = 40,
                             restore_on_miss: bool = True) -> Optional[Tuple]:
        """在消息列表中按谓词查找消息控件，返回 (className, name, rect)。

        RecyclerListView 是虚拟化列表，只实例化可视区约 12 条；历史消息需
        滚动。ScrollPattern 不可用（返回空），改用鼠标滚轮驱动列表滚动。
        match_last=True 时滚到底后从底部向上找，用于取最新表情/消息。

        ``restore_on_miss``：找不到时把列表滚回**最新消息**位置。旧实现
        最多滚 12 次（甚至 60 次）向历史，失败后直接 return，聊天面板就
        停在历史消息处：随后输入框探测失败、右键点到历史消息、OCR 找不到
        「引用」菜单，且会污染下一次发送。只有找不到时才复位——找到时右键
        菜单已弹出，滚轮会把菜单关掉；发送后微信自身会跳回最新。
        """
        lst = self._message_list()
        if lst is None:
            return None
        scrolls = [0]       # 累计「滚向历史」的滚轮次数，供复位用

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
                if direction == 'down':
                    scrolls[0] += times
                for _ in range(times):
                    self._mouse_wheel(delta)
                    time.sleep(0.2)
            except Exception:
                pass

        def restore() -> None:
            """快速滚回最新（不逐次等 0.2s，避免 120 次滚动耗掉 24 秒）。"""
            n = min(scrolls[0] + 2, 160)
            if n <= 2:
                return
            try:
                r = lst.BoundingRectangle
                self._set_cursor((r.left + r.right) // 2,
                                 (r.top + r.bottom) // 2)
                for _ in range(n):
                    self._mouse_wheel(-120)
                    time.sleep(0.02)
                time.sleep(0.25)
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
            if restore_on_miss:
                restore()
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
        if restore_on_miss:
            restore()
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
