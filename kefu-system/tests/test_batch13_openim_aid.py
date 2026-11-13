"""企业微信身份校验（批次 13 追加）：标题区 AID 适配 + 控件树就绪门禁。

现场问题（2026-11-13 19:01 日志）：回复「安佳胤@我的车车」时提示
「无法定位会话」，但微信明明已经打开到那个会话、只是没往输入框里写字，
且「微信到前台 → 开始打字」中间空转 25 秒。两条根因：

1. 标题区企业名标签的 AutomationId 写死在微信 4.0 的布局上
   （``top_content_h_view...current_chat_openim_name``），4.1 起整段搬到
   ``content_view.top_content_view.title_h_view...openim_name_view``，
   于是 ``is_openim_chat()`` 永远 False → 所有企业微信联系人都被判「同名
   普通好友」拒发。
2. ``ensure_window`` 只要看到窗口就返回 True，而控件树被微信回收后只剩
   Qt 外壳（2 个子控件），随后搜搜索框全超时，最终报成「无法定位会话」；
   真正等树物化的 25 秒还压在了用户点发送的那一刻。
"""
from __future__ import annotations

import os
import sys

# 仓库根（wechatauto 包父级）——与 test_batch13_media_key.py 同一写法
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from wechatauto.guia import WeChatGUI
from wechatauto.uia_driver import (MAIN_VIEW_AID, OPENIM_NAME_AIDS,
                                   TITLE_VIEW_AID, WeChatUIA)

NEW_AID, OLD_AID = OPENIM_NAME_AIDS


# ---------------------------------------------------------------------------
# UIA 控件树替身
# ---------------------------------------------------------------------------
class _Ctrl:
    def __init__(self, name: str = "", aid: str = "", children=None) -> None:
        self.Name = name
        self.AutomationId = aid
        self._children = list(children or [])

    def Exists(self, *args, **kwargs) -> bool:
        return True

    def GetChildren(self):
        return list(self._children)


class _Missing(_Ctrl):
    def Exists(self, *args, **kwargs) -> bool:
        return False


class _Win(_Ctrl):
    """最小窗口替身：只实现 openim_label / is_ready 用到的那几个查找。"""

    def __init__(self, texts=None, title=None, mainview=True) -> None:
        super().__init__(name="微信")
        self._texts = dict(texts or {})
        self._title = title
        self._mainview = mainview

    def TextControl(self, AutomationId: str = "", **kw):  # noqa: N802
        if AutomationId in self._texts:
            return _Ctrl(self._texts[AutomationId], AutomationId)
        return _Missing()

    def GroupControl(self, AutomationId: str = "", **kw):  # noqa: N802
        if AutomationId == MAIN_VIEW_AID:
            return _Ctrl() if self._mainview else _Missing()
        if AutomationId == TITLE_VIEW_AID and self._title is not None:
            return self._title
        return _Missing()


def _uia(win) -> WeChatUIA:
    uia = WeChatUIA()
    uia._win = win
    uia.ensure_window = lambda *a, **k: True   # type: ignore[assignment]
    return uia


def _raw_uia(win) -> WeChatUIA:
    """不替换 ensure_window：给「窗口/控件树门禁」那几个用例用。"""
    uia = WeChatUIA()
    uia._win = win
    uia._find_main = lambda: win               # type: ignore[assignment]
    uia._activate = lambda w: None             # type: ignore[assignment]
    return uia


# ---------------------------------------------------------------------------
# 1. 企业名标签：新旧 AID + 子树兜底
# ---------------------------------------------------------------------------
class TestOpenimLabel:
    def test_new_layout_aid_hits(self):
        """微信 4.1 实测布局（本机 2026-11-13 真实控件路径）。"""
        uia = _uia(_Win(texts={NEW_AID: "@我的车车"}))
        assert uia.openim_label() == "@我的车车"
        assert uia.is_openim_chat() is True

    def test_legacy_layout_aid_still_works(self):
        """旧版微信（4.0）路径必须继续可用，不能升级一次废掉一批用户。"""
        uia = _uia(_Win(texts={OLD_AID: "@我的车车"}))
        assert uia.openim_label() == "@我的车车"

    def test_title_subtree_scan_when_aid_changes_again(self):
        """AID 又变也不怕：标题区里以 @ 开头的文本就是企业名。"""
        deep = _Ctrl(children=[_Ctrl(children=[_Ctrl("@我的车车")])])
        title = _Ctrl(children=[_Ctrl("安佳胤"), deep])
        uia = _uia(_Win(title=title))
        assert uia.openim_label() == "@我的车车"

    def test_no_label_means_plain_friend(self):
        """标题区在、没有 @ 标签 = 同名普通好友（明确 False，不能放行）。"""
        title = _Ctrl(children=[_Ctrl("安佳胤")])
        uia = _uia(_Win(title=title))
        assert uia.openim_label() == ""
        assert uia.is_openim_chat() is False

    def test_missing_title_view_is_unknown_not_false(self):
        """标题区都拿不到 = 控件树没物化，只能是 None（不该据此拒发）。"""
        uia = _uia(_Win(title=None))
        assert uia.openim_label() is None
        assert uia.is_openim_chat() is None

    def test_no_window_returns_none(self):
        uia = WeChatUIA()
        uia.ensure_window = lambda *a, **k: False  # type: ignore[assignment]
        assert uia.openim_label() is None


# ---------------------------------------------------------------------------
# 2. 控件树就绪门禁
# ---------------------------------------------------------------------------
class TestReadyGate:
    def test_is_ready_requires_main_view(self):
        assert _uia(_Win(mainview=True)).is_ready() is True
        assert _uia(_Win(mainview=False)).is_ready() is False

    def test_ensure_window_fails_when_tree_never_materialises(self):
        win = _Win(mainview=False)
        uia = _raw_uia(win)
        uia.is_ready = lambda: False            # type: ignore[assignment]
        uia._wake_and_wait_ready = (
            lambda *a, **k: False)          # type: ignore[assignment]
        # 窗口在但树不可用 → 必须返回 False，让上层报「正在唤醒」而不是
        # 继续往下走、最后报成「无法定位会话」
        assert uia.ensure_window() is False

    def test_ensure_window_waits_for_tree(self):
        win = _Win()
        uia = _raw_uia(win)
        ready = {"n": 0}

        def fake_ready():
            ready["n"] += 1
            return ready["n"] > 1               # 第一次不可用，唤醒后可用

        uia.is_ready = fake_ready               # type: ignore[assignment]
        uia._wake_and_wait_ready = (
            lambda *a, **k: True)           # type: ignore[assignment]
        assert uia.ensure_window() is True

    def test_warm_tree_does_not_activate_window(self):
        """预热只让树就绪，绝不能把微信窗口抢到前台（用户可能正在干别的）。"""
        win = _Win()
        uia = _raw_uia(win)
        uia.is_ready = lambda: True             # type: ignore[assignment]
        calls = []
        uia._activate = lambda w: calls.append(w)   # type: ignore[assignment]
        assert uia.warm_tree() is True
        assert calls == []

    def test_warm_tree_wakes_when_not_ready(self):
        win = _Win(mainview=False)
        uia = _raw_uia(win)
        uia.is_ready = lambda: False            # type: ignore[assignment]
        woke = []
        uia._wake_and_wait_ready = (
            lambda *a, **k: woke.append(1) or True)  # type: ignore[assignment]
        assert uia.warm_tree() is True
        assert woke == [1]


# ---------------------------------------------------------------------------
# 3. 打开会话的身份校验（guia 层）
# ---------------------------------------------------------------------------
class _FakeUia:
    def __init__(self, *, label=None, opened=True, ready=True,
                 chat="安佳胤") -> None:
        self._label = label
        self._opened = opened
        self._ready = ready
        self._chat = chat
        self.opened_with: list[tuple] = []

    def same_chat(self, a, b) -> bool:
        return a == b

    def current_chat(self):
        return self._chat

    def open_chat(self, name, prefer_openim=False, openim_hint=""):
        self.opened_with.append((name, prefer_openim, openim_hint))
        if self._opened:
            self._chat = name
        return self._opened

    def openim_label(self):
        return self._label

    def is_openim_chat(self):
        return None if self._label is None else bool(self._label)

    def is_ready(self):
        return self._ready


class _GuiStub:
    """不构造真 WeChatGUI（要真微信窗口），只借它的方法体。"""

    _last_open_error = ""
    _current_chat = None
    _uia = None

    open_chat_prefer_openim = WeChatGUI.open_chat_prefer_openim
    last_open_error = WeChatGUI.last_open_error
    _corp_matches = staticmethod(WeChatGUI._corp_matches)

    def _get_uia(self):
        return self._uia

    def open_chat(self, name):
        return True


def _gui(uia) -> _GuiStub:
    g = _GuiStub()
    g._uia = uia
    g._last_open_error = ""
    return g


class TestOpenChatPreferOpenim:
    def test_confirms_when_label_matches(self):
        g = _gui(_FakeUia(label="@我的车车"))
        assert g.open_chat_prefer_openim("安佳胤", "我的车车") is True
        assert g.last_open_error() == ""

    def test_refuses_plain_friend(self):
        """打开后没有企业名标签 = 同名普通好友，必须拒发。"""
        g = _gui(_FakeUia(label=""))
        assert g.open_chat_prefer_openim("安佳胤", "我的车车") is False
        assert g.last_open_error() == "identity_mismatch"

    def test_refuses_other_corp(self):
        g = _gui(_FakeUia(label="@别的公司"))
        assert g.open_chat_prefer_openim("安佳胤", "我的车车") is False
        assert g.last_open_error() == "corp_mismatch"

    def test_unknown_label_is_allowed(self):
        """标签读不到（树没物化）时不武断拒发，但也不能报成身份不符。"""
        g = _gui(_FakeUia(label=None))
        assert g.open_chat_prefer_openim("安佳胤", "我的车车") is True
        assert g.last_open_error() == ""

    def test_reports_tree_not_ready(self):
        g = _gui(_FakeUia(label=None, opened=False, ready=False))
        assert g.open_chat_prefer_openim("安佳胤", "我的车车") is False
        assert g.last_open_error() == "tree_not_ready"

    def test_reuses_already_open_openim_chat(self):
        """用户已经手动打开该企业微信会话时，直接复用、不再搜索。"""
        uia = _FakeUia(label="@我的车车", chat="安佳胤")
        g = _gui(uia)
        assert g.open_chat_prefer_openim("安佳胤", "我的车车") is True
        assert uia.opened_with == []
