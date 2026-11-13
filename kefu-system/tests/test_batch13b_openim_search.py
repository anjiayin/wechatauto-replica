"""企业微信同名联系人：搜索候选与逐个点开验身份（批次 13 补丁 2）。

实机复现（2026-11-13 19:43，只读点开、不发消息）得到的确定结论：

搜索「安佳胤」的真实结果是两条**同名**条目：

===============  =========================  ===================
分区             标题栏 @企业名 标签          实际身份
===============  =========================  ===================
最常使用          ``@我的车车``               **企业微信**（目标）
联系人            空                          普通好友
===============  =========================  ===================

两条的 ``Name`` 与 ``AutomationId`` （``search_item_安佳胤``）**完全相同**，
靠分区名/副标题都判不出来（一条也没标「企业微信」）。而旧实现的
「最常使用/最近使用 与普通分区 aid 重复则去重」恰好把**企业微信那条删掉**，
于是永远只剩普通好友：要么被判 identity_mismatch 拒发，要么发错人。

修法有两条，缺一不可：

1. ``keep_freq=True``：企业微信搜索时不去重，保住「最常使用」里那条；
2. ``_open_matching_openim``：逐个点开、读标题栏的 ``@企业名`` 标签确认，
   读到匹配的那条才算命中（点开只切会话，不输入不发送）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from wechatauto.guia import WeChatGUI
from wechatauto.uia_driver import WeChatUIA

NAME = "安佳胤"
CORP = "我的车车"
AID = "search_item_安佳胤"
SECTIONS = ("最常使用", "最近使用", "联系人", "群聊")


# ---------------------------------------------------------------------------
# 搜索结果列表替身
# ---------------------------------------------------------------------------
class _Cell:
    """搜索结果行；点开时把「当前会话标签」切到预设值。"""

    def __init__(self, name: str, aid: str, label: str, state: dict,
                 section: str) -> None:
        self.Name = name
        self.AutomationId = aid
        self._label = label
        self._state = state
        self._section = section

    def Click(self) -> None:
        self._state["clicked"].append(self._section)
        self._state["label"] = self._label


class _Header:
    """分区标题行（AutomationId 为空，靠它的 Name 给后面的行归区）。"""

    def __init__(self, name: str) -> None:
        self.Name = name
        self.AutomationId = ""


class _SearchList:
    def __init__(self, children) -> None:
        self._children = list(children)

    def GetChildren(self):
        return list(self._children)


def _results(state: dict, order=("最常使用", "联系人"),
             labels=None) -> _SearchList:
    """构造「同名两条」的搜索结果；labels 指定每个分区点开后的标签。"""
    labels = labels or {}
    children = []
    for sec in order:
        children.append(_Header(sec))
        children.append(_Cell(NAME, AID, labels.get(sec, ""), state, sec))
    return _SearchList(children)


def _uia(monkeypatch, listbox, state) -> WeChatUIA:
    """把搜索链路打桩到「一搜就命中、点开即切标签」。"""
    uia = WeChatUIA()
    uia.ensure_window = lambda *a, **k: True            # type: ignore
    uia._win = object()                                 # type: ignore
    uia._search_box = lambda win: object()              # type: ignore
    uia._paste_into = lambda *a, **k: None              # type: ignore
    uia._find_search_list = lambda *a, **k: listbox     # type: ignore
    uia.openim_label = lambda: state["label"]           # type: ignore
    uia.current_chat = lambda: NAME                     # type: ignore
    monkeypatch.setattr("wechatauto.uia_driver.time.sleep",
                        lambda *a, **k: None)
    return uia


def _state() -> dict:
    return {"clicked": [], "label": "", "section": None}


# ---------------------------------------------------------------------------
# 1. 候选采集：企业微信那条不能被 aid 去重吃掉
# ---------------------------------------------------------------------------
class TestCollectKeepsFreq:
    def test_plain_dedup_drops_openim_entry(self, monkeypatch):
        """保持旧行为时的证据：两条同 aid，去重后只剩「联系人」那条。"""
        st = _state()
        uia = _uia(monkeypatch, _results(st), st)
        got = uia._collect_results(NAME, prefer_openim=True, openim_hint=CORP)
        assert [r["section"] for r in got] == ["联系人"]

    def test_keep_freq_keeps_both(self, monkeypatch):
        """keep_freq=True 时两条都留着，才有机会逐条验明身份。"""
        st = _state()
        uia = _uia(monkeypatch, _results(st), st)
        got = uia._collect_results(NAME, prefer_openim=True, openim_hint=CORP,
                                   keep_freq=True)
        assert [r["section"] for r in got] == ["最常使用", "联系人"]

    def test_open_chat_passes_keep_freq_for_openim(self, monkeypatch):
        """open_chat 在企业微信场景必须自己带上 keep_freq，否则第一条就丢。"""
        st = _state()
        box = _results(st, labels={"最常使用": "@我的车车"})
        uia = _uia(monkeypatch, box, st)
        assert uia.open_chat(NAME, prefer_openim=True,
                             openim_hint=CORP) is True
        assert st["clicked"] == ["最常使用"]


# ---------------------------------------------------------------------------
# 2. 逐个点开验标签
# ---------------------------------------------------------------------------
class TestOpenMatchingOpenim:
    def test_hits_freq_entry_first(self, monkeypatch):
        """企业微信那条在「最常使用」：排前面先试，一次点开即中。"""
        st = _state()
        box = _results(st, labels={"最常使用": "@我的车车"})
        uia = _uia(monkeypatch, box, st)
        assert uia.open_chat(NAME, prefer_openim=True,
                             openim_hint=CORP) is True
        assert st["clicked"] == ["最常使用"]

    def test_skips_plain_friend_then_finds_corp(self, monkeypatch):
        """先点开的若是同名普通好友（标签为空），必须继续试下一条。"""
        st = _state()
        box = _results(st, labels={"联系人": "@我的车车"})
        uia = _uia(monkeypatch, box, st)
        assert uia.open_chat(NAME, prefer_openim=True,
                             openim_hint=CORP) is True
        # 「最常使用」先试（普通好友）→ 再试「联系人」（企业微信）
        assert st["clicked"] == ["最常使用", "联系人"]

    def test_all_plain_friends_returns_false(self, monkeypatch):
        """同名会话全都不是这家企业的 → 不能放行（宁可不发，也不发错人）。"""
        st = _state()
        uia = _uia(monkeypatch, _results(st), st)
        assert uia.open_chat(NAME, prefer_openim=True,
                             openim_hint=CORP) is False
        assert st["clicked"] == ["最常使用", "联系人"]

    def test_other_corp_is_not_accepted(self, monkeypatch):
        """同名的**别家企业**联系人也必须拒（发错公司同样算发错人）。"""
        st = _state()
        box = _results(st, labels={"最常使用": "@别的公司"})
        uia = _uia(monkeypatch, box, st)
        assert uia.open_chat(NAME, prefer_openim=True,
                             openim_hint=CORP) is False

    def test_without_hint_keeps_old_behaviour(self, monkeypatch):
        """不知道企业名时无法验标签，保持原「取第一条」行为。"""
        st = _state()
        uia = _uia(monkeypatch, _results(st), st)
        assert uia.open_chat(NAME, prefer_openim=True) is True
        assert st["clicked"] == ["最常使用"]


# ---------------------------------------------------------------------------
# 3. 企业名标签匹配
# ---------------------------------------------------------------------------
class TestCorpInLabel:
    def test_strips_at_prefix(self):
        assert WeChatUIA._corp_in_label("@我的车车", "我的车车") is True

    def test_substring_either_way(self):
        assert WeChatUIA._corp_in_label("@我的车车(北京)", "我的车车") is True
        assert WeChatUIA._corp_in_label("@我的车车", "我的车车网络") is True

    def test_rejects_empty_and_mismatch(self):
        assert WeChatUIA._corp_in_label("", CORP) is False
        assert WeChatUIA._corp_in_label(None, CORP) is False
        assert WeChatUIA._corp_in_label("@我的车车", "") is False
        assert WeChatUIA._corp_in_label("@别的公司", CORP) is False


# ---------------------------------------------------------------------------
# 4. guia 层：把失败原因讲清楚
# ---------------------------------------------------------------------------
class _FakeUia:
    def __init__(self, *, opened=False, ready=True, label="", chat="别人"):
        self._opened = opened
        self._ready = ready
        self._label = label
        self._chat = chat
        self.calls = []

    def same_chat(self, a, b) -> bool:
        return a == b

    def current_chat(self):
        return self._chat

    def open_chat(self, name, prefer_openim=False, openim_hint=""):
        self.calls.append((name, prefer_openim, openim_hint))
        return self._opened

    def openim_label(self):
        return self._label

    def is_ready(self):
        return self._ready


class _GuiStub:
    _last_open_error = ""
    _current_chat = None
    _uia = None

    open_chat_prefer_openim = WeChatGUI.open_chat_prefer_openim
    last_open_error = WeChatGUI.last_open_error
    _corp_matches = staticmethod(WeChatGUI._corp_matches)

    def _get_uia(self):
        return self._uia


def _gui(uia) -> _GuiStub:
    g = _GuiStub()
    g._uia = uia
    g._last_open_error = ""
    g._current_chat = None
    return g


class TestOpenFailureReason:
    def test_tree_ready_but_no_match_is_identity_mismatch(self):
        """树是好的、只是没这家企业的同名会话：说「身份不符」而不是「树没好」。"""
        gui = _gui(_FakeUia(opened=False, ready=True))
        assert gui.open_chat_prefer_openim(NAME, CORP) is False
        assert gui.last_open_error() == "identity_mismatch"

    def test_tree_not_ready_reported_separately(self):
        gui = _gui(_FakeUia(opened=False, ready=False))
        assert gui.open_chat_prefer_openim(NAME, CORP) is False
        assert gui.last_open_error() == "tree_not_ready"

    def test_other_corp_label_is_corp_mismatch(self):
        gui = _gui(_FakeUia(opened=True, ready=True, label="@别的公司",
                            chat=NAME))
        assert gui.open_chat_prefer_openim(NAME, CORP) is False
        assert gui.last_open_error() == "corp_mismatch"
