# -*- coding: utf-8 -*-
"""非聊天页 / 预览窗遮挡下的会话定位回归（2026-11-28 反馈）。

实机日志（1.4.6，15:26~15:27，打包版）：

    guia.py:1766 打开会话 安佳胤 失败（UIA 路径，原因=未搜到目标）

用户当时微信停在收藏/发现等非聊天页面（朋友圈还打开了图片预览浮窗）：
左栏没有全局搜索框或搜索只在当前页面内生效 → 永远搜不到联系人；预览
浮窗盖在主窗口上时，物理点击也全部落到浮窗上。

修法：
  1. open_chat 搜索前先切回「聊天」页（MainTabBar 的聊天按钮，优先
     Invoke，需退回物理点击）；
  2. 搜索框缺失或首轮空结果时强制再归一页面并重搜一轮；
  3. 发送前关闭同进程的 PreviewWindow 预览浮窗（WM_CLOSE），避免点击
     被遮挡。

本测试全部使用受控桩，不碰真实微信窗口。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from wechatauto.uia_driver import WeChatUIA  # noqa: E402

NAME = "安佳胤"


# ---------------------------------------------------------------------------
# 主窗口 / 顶栏 / 预览窗替身
# ---------------------------------------------------------------------------
class _State:
    def __init__(self) -> None:
        self.page = "favorites"     # 当前页面：favorites / chat
        self.actions = []           # 页面切换动作顺序（invoke / click）
        self.invokable = True       # 聊天按钮是否支持 Invoke


class _InvokePattern:
    def __init__(self, state: _State) -> None:
        self._state = state

    def Invoke(self) -> None:
        self._state.actions.append("invoke")
        self._state.page = "chat"


class _Button:
    ControlTypeName = "ButtonControl"

    def __init__(self, name: str, state: _State) -> None:
        self.Name = name
        self._state = state

    def Exists(self, *a, **k) -> bool:
        return True

    def GetInvokePattern(self):
        if not self._state.invokable:
            raise RuntimeError("该按钮不支持 Invoke")
        return _InvokePattern(self._state)

    def Click(self) -> None:
        self._state.actions.append("click")
        self._state.page = "chat"


class _TabBar:
    def __init__(self, state: _State) -> None:
        self._state = state

    def Exists(self, *a, **k) -> bool:
        return True

    def ButtonControl(self, **kw):
        return _Button(kw.get("Name") or "", self._state)

    def GetChildren(self):
        return [_Button("聊天", self._state)]


class _PageProbe:
    def __init__(self, state: _State) -> None:
        self._state = state

    def Exists(self, *a, **k) -> bool:
        return self._state.page == "chat"


class _Win:
    def __init__(self, state: _State) -> None:
        self._state = state

    def GroupControl(self, **kw):
        return _PageProbe(self._state)

    def ToolBarControl(self, **kw):
        return _TabBar(self._state)


# ---------------------------------------------------------------------------
# 1. 页面归一：_ensure_chat_tab
# ---------------------------------------------------------------------------
class TestEnsureChatTab:
    def test_already_on_chat_page_no_action(self):
        st = _State()
        st.page = "chat"
        uia = WeChatUIA()
        uia._win = _Win(st)  # type: ignore[assignment]
        assert uia._ensure_chat_tab() is True
        assert st.actions == []

    def test_switches_via_invoke(self):
        st = _State()
        uia = WeChatUIA()
        uia._win = _Win(st)  # type: ignore[assignment]
        assert uia._chat_page_visible(uia._win) is False
        assert uia._ensure_chat_tab() is True
        assert st.page == "chat"
        assert st.actions == ["invoke"]

    def test_falls_back_to_click_without_invoke(self):
        st = _State()
        st.invokable = False
        uia = WeChatUIA()
        uia._win = _Win(st)  # type: ignore[assignment]
        assert uia._ensure_chat_tab() is True
        assert st.actions == ["click"]


# ---------------------------------------------------------------------------
# 2. open_chat：空结果后强制切页重搜 / 搜索框缺失恢复 / 预览窗先清理
# ---------------------------------------------------------------------------
class _Cell:
    def __init__(self, state: dict) -> None:
        self._state = state

    def Click(self) -> None:
        self._state["opened"] = True


def _make_uia(monkeypatch, state: dict, calls: dict,
              on_chat_after_force: bool = True,
              box_sequence=None) -> WeChatUIA:
    """打桩 open_chat 依赖：窗口/搜索框/粘贴/采集/校验。"""
    uia = WeChatUIA()
    uia.ensure_window = lambda *a, **k: True            # type: ignore
    uia._win = object()                                 # type: ignore

    def fake_dismiss() -> int:
        calls["order"].append("preview")
        return 1

    def fake_ensure(win=None, force=False) -> bool:
        calls["tab"].append(bool(force))
        if force and on_chat_after_force:
            state["switched"] = True
            return True
        return state["switched"]

    def fake_search_box(win):
        if box_sequence is not None:
            return next(box_sequence, None)
        return object()

    def fake_paste(box, text: str, clear: bool = True) -> None:
        calls["order"].append("paste")
        calls["paste"].append(text)

    def fake_collect(kw, **kwargs):
        calls["order"].append("collect")
        calls["search"] += 1
        if not state["switched"]:
            return []
        return [{"name": kw, "section": "联系人",
                 "aid": "search_item_" + kw, "cell": _Cell(state),
                 "index": 0}]

    uia._dismiss_preview_windows = fake_dismiss             # type: ignore
    uia._ensure_chat_tab = fake_ensure                      # type: ignore
    uia._search_box = fake_search_box                       # type: ignore
    uia._paste_into = fake_paste                            # type: ignore
    uia._collect_results = fake_collect                     # type: ignore
    uia.current_chat = lambda: NAME if state["opened"] else None  # type: ignore
    monkeypatch.setattr("wechatauto.uia_driver.time.sleep",
                        lambda *a, **k: None)
    return uia


class TestOpenChatRecovery:
    def test_retries_search_after_switching_back_to_chat_page(self, monkeypatch):
        """首轮在非聊天页搜空 → 强制切页 → 重搜命中 → 打开成功。"""
        state = {"switched": False, "opened": False}
        calls = {"order": [], "tab": [], "paste": [], "search": 0}
        uia = _make_uia(monkeypatch, state, calls)
        assert uia.open_chat(NAME, retries=1) is True
        assert state["opened"] is True
        # 首次搜索前、以及空结果后重搜前，各做过一次页面归一
        assert calls["tab"] == [False, True]
        assert calls["search"] == 2
        assert calls["paste"] == [NAME, NAME]
        # 预览窗清理必须发生在任何搜索/剪贴板操作之前
        assert calls["order"][0] == "preview"

    def test_box_missing_triggers_second_page_normalize(self, monkeypatch):
        """搜索框缺失（收藏页常见）→ 切页后重找 → 正常搜索命中。"""
        state = {"switched": True, "opened": False}
        calls = {"order": [], "tab": [], "paste": [], "search": 0}
        boxes = iter([None, object()])
        uia = _make_uia(monkeypatch, state, calls, box_sequence=boxes)
        assert uia.open_chat(NAME) is True
        assert calls["tab"] == [False, True]
        assert calls["search"] == 1

    def test_returns_false_when_box_never_appears(self, monkeypatch):
        """切页后仍找不到搜索框：保持 fail-closed，不空转。"""
        state = {"switched": False, "opened": False}
        calls = {"order": [], "tab": [], "paste": [], "search": 0}
        boxes = iter([None, None])
        uia = _make_uia(monkeypatch, state, calls, box_sequence=boxes)
        assert uia.open_chat(NAME) is False
        assert calls["tab"] == [False, True]
        assert calls["search"] == 0