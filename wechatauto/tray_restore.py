# -*- coding: utf-8 -*-
"""通过单击（必要时双击）系统托盘图标，让 Qt 自己把微信主窗口 show 出来。

背景（2026-11-15 实机验证）
--------------------------
微信「关闭到托盘」是 Qt 自己 ``hide()``（``IsWindowVisible=False``），
与 Win32 最小化（``IsIconic=True`` 但 visible=True）完全不同：

* ``ShowWindow(SW_SHOW)`` **无效且有害**：Qt 内部仍是托盘态，UIA 树不
  物化，窗口会卡成 ``visible=True + iconic=True`` 的 -32000 伪矩形
  （11:33 实测「界面不能动」）；反复写 accessibility gate 还会把 Qt
  输入系统写挂。
* 再启动一次 ``Weixin.exe`` **也不是唤起**：18:53 实测新起 PID 10404、
  弹出一个 296x388 的新窗口（疑似登录窗）后自行退出，旧主窗仍在托盘。
* 唯一可靠做法 = **模拟用户点击托盘图标**：explorer 把托盘回调消息发给
  ``WxTrayIconMessageWindow``，微信自己调 ``show()``。18:56 实测单击
  溢出区的「微信」图标后，主窗 592108 由 visible=False 变 True、
  ``iconic=False``、rect 保持 ``(106,130,907,809)``、PID 不变。
  19:03 又发现两个坑：溢出面板里可能同时有同名的 37x22 文本标签和
  40x40 图标按钮（必须按面积选大的，点到标签上单击无效）；且单击并非
  总生效，**双击**（WM_LBUTTONDBLCLK）才是稳定打开动作 → 单击 0.8s
  没唤起就补一次双击。19:05 端到端实测 2.62s 唤起并继续发送前置。

实现约束
--------
* 绝不写死坐标：V 形按钮与托盘图标都从 UIA ``BoundingRectangle`` 现取，
  兼容任务栏位置/分辨率/DPI 变化；图标可能在可见通知区，也可能在
  「通知 V 形」溢出面板里，两处都要找。
* 图标名称**精确匹配**（``微信``/``Weixin``/``WeChat``），绝不点任务栏
  那个「微信 - 1 个运行窗口」按钮，也不点相邻的其他托盘图标。
* 全程不调用 ``ShowWindow``、不写进程内存、不启动新进程；点完恢复鼠标
  位置并收起溢出面板。
* 找不到图标 / 点了没起来 → 返回 False，调用方按 fail-closed 处理。
"""
from __future__ import annotations

import time
from typing import List, Optional

from wechatauto.logger import wxlog

try:
    import win32gui
    import win32api
    import win32con
    import uiautomation as auto
    _HAS_UIA = True
except Exception:                                   # pragma: no cover
    _HAS_UIA = False

# 托盘图标显示名（中文版微信 / 英文版 / 部分版本带 Weixin 前缀）
TRAY_ICON_NAMES = frozenset(("微信", "Weixin", "WeChat"))
# 「显示隐藏图标」按钮：Win10 通知区 V 形的 AutomationId
CHEVRON_AIDS = frozenset(("1502",))
CHEVRON_NAME_HINTS = ("V 形", "V形", "隐藏图标", "hidden icons", "Show Hidden Icons")

_SHELL_CLASS = "Shell_TrayWnd"
_OVERFLOW_CLASS = "NotifyIconOverflowWindow"


def _top_level(class_name: str) -> List[int]:
    """枚举指定类名的顶层窗口句柄。"""
    found: List[int] = []

    def cb(h, _):
        try:
            if win32gui.GetClassName(h) == class_name:
                found.append(h)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
    return found


def _children(root, max_depth: int = 6) -> list:
    """浅层遍历 UIA 子树（托盘区层级很浅，不做全树搜索）。"""
    out: list = []

    def walk(node, depth: int):
        if depth > max_depth:
            return
        try:
            kids = node.GetChildren()
        except Exception:
            return
        for kid in kids:
            out.append(kid)
            walk(kid, depth + 1)

    if root is not None:
        walk(root, 0)
    return out


def _control_from(hwnd: int):
    try:
        return auto.ControlFromHandle(hwnd)
    except Exception:
        return None


def _box(rect):
    """把 UIA BoundingRectangle 转成 (left, top, right, bottom)。"""
    try:
        return (int(rect.left), int(rect.top),
                int(rect.right), int(rect.bottom))
    except Exception:
        return None


def _center(rect):
    box = _box(rect)
    if not box:
        return None
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return None
    return ((x0 + x1) // 2, (y0 + y1) // 2)


def _press(x: int, y: int, times: int = 1) -> None:
    """真实鼠标左键单击 / 双击（托盘图标只认真实输入，UIA Invoke 无效）。

    ``times=2`` 必须在系统双击阈值内连按，不能拆成两次 ``_click``
    （中间会插入 SetCursorPos + sleep，容易被判成两次单击）。
    """
    win32api.SetCursorPos((x, y))
    time.sleep(0.10)
    for _ in range(times):
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)
        time.sleep(0.08)
    time.sleep(0.08)


def _click(x: int, y: int) -> None:
    _press(x, y, 1)


def _double_click(x: int, y: int) -> None:
    _press(x, y, 2)


def _find_icon(roots: list):
    """在托盘区控件树里按精确名称找微信**图标按钮**。

    19:03 实测：溢出面板里可能同时有 40x40 的图标按钮和 37x22 的同名
    文本标签（Name 都是「微信」）。取第一个匹配会点到标签上——单击不
    生效，白等 3 秒。按矩形面积选最大的那个才是图标按钮。
    """
    hits = []
    for root in roots:
        for ctrl in _children(root):
            try:
                if (ctrl.Name or "").strip() in TRAY_ICON_NAMES:
                    hits.append(ctrl)
            except Exception:
                pass
    if not hits:
        return None

    def _area(ctrl):
        try:
            box = _box(ctrl.BoundingRectangle)
            return (box[2] - box[0]) * (box[3] - box[1]) if box else 0
        except Exception:
            return 0

    return max(hits, key=_area)


def _find_chevron():
    """找「显示隐藏图标」V 形按钮。"""
    for hwnd in _top_level(_SHELL_CLASS):
        for ctrl in _children(_control_from(hwnd)):
            try:
                aid = str(ctrl.AutomationId or "")
                name = ctrl.Name or ""
                if aid in CHEVRON_AIDS or any(h in name for h in CHEVRON_NAME_HINTS):
                    return ctrl
            except Exception:
                pass
    return None


def _overflow_roots(visible_only: bool = True) -> list:
    roots = []
    for hwnd in _top_level(_OVERFLOW_CLASS):
        if visible_only and not win32gui.IsWindowVisible(hwnd):
            continue
        roots.append(_control_from(hwnd))
    return [r for r in roots if r is not None]


def restore_wechat_from_tray(main_hwnd: int, timeout: float = 3.0) -> bool:
    """点击微信托盘图标，等 Qt 自己把 ``main_hwnd`` 显示出来。

    先单击（选面积最大的图标按钮，避开同名文本标签）；0.8 秒没唤起就
    补一次双击，兼容不同通知区实现。

    Args:
        main_hwnd: 已知的微信主窗口句柄（托盘期间句柄仍有效）。
        timeout: 单击+双击后等待窗口变为「可见且非最小化」的总秒数。

    Returns:
        True = 主窗口已由微信自己恢复可见；False = 找不到图标 / 点击
        失败 / 超时，调用方应继续 fail-closed（不 ``SW_SHOW``、不写补丁）。
    """
    if not _HAS_UIA or not main_hwnd:
        return False

    def _visible() -> bool:
        try:
            return (bool(win32gui.IsWindowVisible(main_hwnd))
                    and not bool(win32gui.IsIconic(main_hwnd)))
        except Exception:
            return False

    if _visible():
        return True                       # 已经可见，不需要动桌面

    old_cursor = None
    expanded = False
    try:
        try:
            old_cursor = win32api.GetCursorPos()
        except Exception:
            pass

        shells = _top_level(_SHELL_CLASS)
        if not shells:
            wxlog.warning('未找到系统任务栏窗口，托盘图标唤起放弃')
            return False
        shell_roots = [r for r in (_control_from(h) for h in shells)
                       if r is not None]
        icon = _find_icon(shell_roots)    # ① 图标在可见通知区

        if icon is None:                  # ② 图标在「通知 V 形」溢出区
            chevron = _find_chevron()
            if chevron is None:
                wxlog.warning('未找到微信托盘图标，也未找到「显示隐藏图标」按钮；'
                              '放弃自动唤起')
                return False
            pt = _center(chevron.BoundingRectangle)
            if pt is None:
                return False
            _click(*pt)
            expanded = True
            # 溢出面板是动画展开的，UIA 子节点要几百毫秒到 1 秒才物化；
            # 固定 sleep 0.6s 实测会扑空（19:01:03 自动唤起失败），改成
            # 轮询等待，并在「面板根本没展开」时补点一次 V 形。
            expand_start = time.monotonic()
            deadline = expand_start + 2.0
            retried = False
            while time.monotonic() < deadline:
                roots = _overflow_roots()
                icon = _find_icon(roots)
                if icon is not None:
                    break
                # 只在「面板持续 1 秒都没出现」时才补点一次，避免展开动画
                # 期间的误判把刚展开的面板又收回去（会点到空处）。
                if not roots and not retried and time.monotonic() - expand_start > 1.0:
                    _click(*pt)
                    retried = True
                time.sleep(0.2)

        if icon is None:
            wxlog.warning('托盘区未找到「微信」图标，放弃自动唤起'
                          '（不猜坐标、不点其他图标）')
            return False
        pt = _center(icon.BoundingRectangle)
        if pt is None:
            return False

        def _wait(sec: float) -> bool:
            end = time.monotonic() + sec
            while time.monotonic() < end:
                if _visible():
                    return True
                time.sleep(0.2)
            return _visible()

        _click(*pt)
        if _wait(0.8):
            wxlog.info('已单击微信托盘图标，主窗口自动恢复可见 hwnd=%s',
                       main_hwnd)
            return True

        # 19:03 实测：单击并非总生效（点中同名文本标签 / 通知区实现差异
        # 时 3 秒无反应，而双击 0.5 秒就把窗口 show 出来了）。补一次标准
        # 双击；若溢出面板已收起则重新展开并**重新取图标坐标**，绝不沿用
        # 可能已失效的旧矩形。
        wxlog.info('单击托盘图标未唤起主窗口，补一次双击')
        double_pt = pt
        if expanded:
            if not _overflow_roots():
                ch = _find_chevron()
                ch_pt = _center(ch.BoundingRectangle) if ch else None
                if ch_pt:
                    _click(*ch_pt)
                    time.sleep(0.6)
            again = _find_icon(_overflow_roots())
            if again:
                double_pt = _center(again.BoundingRectangle) or pt
        _double_click(*double_pt)
        if _wait(max(0.4, timeout - 0.8)):
            wxlog.info('已双击微信托盘图标，主窗口自动恢复可见 hwnd=%s',
                       main_hwnd)
            return True
        wxlog.warning('单击+双击微信托盘图标后主窗口仍未可见（共等 %.1fs），'
                      '本次按失败处理 hwnd=%s', timeout, main_hwnd)
        return False
    finally:
        # 收起我展开的溢出面板、把鼠标还给用户（顺序不能反）
        if expanded:
            try:
                if _top_level(_OVERFLOW_CLASS) and any(
                        win32gui.IsWindowVisible(h)
                        for h in _top_level(_OVERFLOW_CLASS)):
                    chevron = _find_chevron()
                    pt = _center(chevron.BoundingRectangle) if chevron else None
                    if pt:
                        _click(*pt)
            except Exception as exc:
                wxlog.debug('收起托盘溢出面板异常（忽略）：%s', exc)
        if old_cursor:
            try:
                win32api.SetCursorPos(old_cursor)
            except Exception:
                pass
