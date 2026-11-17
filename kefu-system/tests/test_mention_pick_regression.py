# -*- coding: utf-8 -*-
"""群 @ 成员选择回归（2026-11-17 商机 #197「@发送人」发送失败）。

事故证据
--------
``wechatauto_logs/app_20261117.log:31``：
``[uia_driver.py:1931] @ 成员未在弹层中选中：安佳胤``，随后 3 秒报
「@ 成员操作未完成，已取消发送」。输入发送耗时 4.9s，正好是
``_mention_popup(2.0)`` + 回退 ``_mention_popup(1.5)`` 两次超时之和。

根因
----
``_mention_popup`` 只按 ``WindowControl`` 找弹层；Qt 弹层以 Pane/Custom
暴露时三个变体全部落空（同 codebase 的 ``mmui::XMenuView`` 就只能靠树遍历
按 ClassName 命中）。``_pick_mention`` 又只读 ListControl 的直接子节点，
成员项被包深一层或 Name 带 U+2005 时匹配不上。历史上 UIA 失败会降级 OCR，
``affb7bd`` 关闭 OCR 兜底后该缺陷直接暴露为发送失败。

本文件全部用受控桩，不触碰真实微信群 / 联系人；只验证定位回退、归一化
匹配和 fail-closed（失败不得静默成功、不得走 OCR）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import pytest

from wechatauto.guia import WeChatGUI
from wechatauto.uia_driver import WeChatUIA


class _FakeItem:
    """成员项替身：只记录是否被点击。"""

    def __init__(self, name: str):
        self.Name = name
        self.clicked = 0

    def Click(self) -> None:
        self.clicked += 1


class _Probe:
    """Exists() 替身：命中目标时才算存在。"""

    def __init__(self, target):
        self._target = target
        self.Name = getattr(target, "Name", "")

    def Exists(self, *args, **kwargs) -> bool:
        return self._target is not None

    def Click(self) -> None:
        if self._target is not None:
            self._target.Click()


class _FakeList:
    def __init__(self, children):
        self._children = children

    def Exists(self, *args, **kwargs) -> bool:
        return True

    def GetChildren(self):
        return list(self._children)


class _FakePop:
    """弹层替身：直接子节点为空，成员项只能靠后代 ListItem 搜索拿到。"""

    def __init__(self, children=None, named_items=None):
        self._list = _FakeList(children or [])
        self._named_items = named_items or {}
        self.roll_calls = 0

    def ListControl(self):
        return self._list

    def ListItemControl(self, Name=None, **kwargs):
        return _Probe(self._named_items.get(Name))

    def GetChildren(self):
        return []


class _FakeNode:
    """控件树节点替身，供 ClassName/AutomationId 遍历使用。"""

    def __init__(self, name="", cls="", aid="", children=None):
        self.Name = name
        self.ClassName = cls
        self.AutomationId = aid
        self._children = children or []

    def GetChildren(self):
        return list(self._children)


class _FakeWin:
    def __init__(self, window_hit=None, control_hit=None, tree=None):
        self.window_probe = _Probe(window_hit)
        self.control_probe = _Probe(control_hit)
        self._tree = tree or []

    def WindowControl(self, **kwargs):
        return self.window_probe

    def Control(self, **kwargs):
        return self.control_probe

    def GetChildren(self):
        return list(self._tree)


@pytest.fixture
def uia():
    return object.__new__(WeChatUIA)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """所有等待都去掉，测试不真的等 2s/1.5s。"""
    monkeypatch.setattr("wechatauto.uia_driver.time.sleep", lambda s: None)


class TestMentionPopupLocating:
    def test_windowcontrol_hit_still_wins(self, uia):
        """原快路径不能破坏：WindowControl 命中即返回。"""
        win = _FakeWin(window_hit=object())
        uia._win = win
        assert uia._mention_popup(timeout=0.05) is win.window_probe

    def test_generic_control_fallback(self, uia):
        """弹层以 Pane/Custom 暴露时，通用 Control 搜索应命中。"""
        win = _FakeWin(window_hit=None, control_hit=object())
        uia._win = win
        assert uia._mention_popup(timeout=0.05) is win.control_probe

    def test_tree_walk_fallback(self, uia):
        """前两类搜索全落空时，按 AutomationId 树遍历应命中。"""
        pop = _FakeNode(aid="MentionPopover")
        uia._win = _FakeWin(
            window_hit=None,
            control_hit=None,
            tree=[_FakeNode(cls="mmui::ChatView", children=[pop])],
        )
        assert uia._mention_popup(timeout=0.05) is pop

    def test_no_popup_returns_none(self, uia):
        uia._win = _FakeWin()
        assert uia._mention_popup(timeout=0.05) is None


class TestPickMention:
    def test_nested_listitem_is_clicked(self, uia, monkeypatch):
        """成员项不在 ListControl 直接子节点时，后代搜索仍应选中。"""
        item = _FakeItem("安佳胤")
        pop = _FakePop(children=[], named_items={"安佳胤": item})
        monkeypatch.setattr(uia, "_mention_popup", lambda timeout=2.0: pop)

        assert uia._pick_mention("安佳胤") is True
        assert item.clicked == 1

    def test_u2005_whitespace_is_normalized(self, uia, monkeypatch):
        """微信 @ 提及带 U+2005，归一化后必须能匹配。"""
        item = _FakeItem("安佳胤\u2005(家人)")
        pop = _FakePop(children=[item])
        monkeypatch.setattr(uia, "_mention_popup", lambda timeout=2.0: pop)

        assert uia._pick_mention("安佳胤") is True
        assert item.clicked == 1

    def test_exact_direct_child_still_works(self, uia, monkeypatch):
        """原「直接子节点精确匹配」路径保持兼容。"""
        item = _FakeItem("张三")
        pop = _FakePop(children=[item])
        monkeypatch.setattr(uia, "_mention_popup", lambda timeout=2.0: pop)

        assert uia._pick_mention("张三") is True
        assert item.clicked == 1

    def test_no_popup_fails_closed(self, uia, monkeypatch):
        """弹层定位不到必须返回 False，绝不静默成功。"""
        monkeypatch.setattr(uia, "_mention_popup", lambda timeout=2.0: None)
        assert uia._pick_mention("安佳胤") is False

    def test_no_matching_item_fails_closed(self, uia, monkeypatch):
        """读到了列表但成员名对不上：返回 False，不点任何项。"""
        pop = _FakePop(children=[_FakeItem("张三"), _FakeItem("李四")])
        monkeypatch.setattr(uia, "_mention_popup", lambda timeout=2.0: pop)
        assert uia._pick_mention("安佳胤") is False


class TestGuiFailClosed:
    def test_failed_uia_at_reported_without_ocr(self, monkeypatch):
        """UIA 选人失败时 guia 必须如实失败，且不再走 OCR 兜底。"""
        gui = object.__new__(WeChatGUI)

        class _FakeUia:
            def current_chat(self):
                return "家人群"

            def same_chat(self, cur, target):
                return cur == target

            def at_members(self, names, text, **kwargs):
                return False

        monkeypatch.setattr(gui, "_require_uia", lambda op: (_FakeUia(), None))
        monkeypatch.setattr(gui, "_confirm_target_chat", lambda *a, **kw: True)

        def _should_not_call_ocr(*args, **kwargs):
            raise AssertionError("OCR 兜底必须保持关闭")

        monkeypatch.setattr(gui, "_at_member_ocr", _should_not_call_ocr)

        resp = gui.at_members(["安佳胤"], "收到", who="家人群", verify=False)
        assert resp.is_success is False
        assert "@ 成员操作未完成" in str(resp.get("message", ""))
        assert resp.get("data", {}).get("chat_verified") is False


class TestSingleItemGuard:
    """唯一项兜底收紧（安全加固）：未过滤拒绝、已过滤仍受字符关联守卫。"""

    @staticmethod
    def _wire(uia, monkeypatch, pop):
        monkeypatch.setattr(uia, "_mention_popup", lambda timeout=2.0: pop)

    def test_unfiltered_single_unrelated_item_is_rejected(
            self, uia, monkeypatch):
        """未过滤：唯一项与目标完全无共享字符 → 不点击、返回 False。"""
        item = _FakeItem("张三")
        self._wire(uia, monkeypatch, _FakePop(children=[item]))

        assert uia._pick_mention("安佳胤") is False
        assert item.clicked == 0

    def test_unfiltered_single_exact_still_matches(self, uia, monkeypatch):
        """未过滤：唯一项精确命中时正常点击（守卫只约束兜底路径）。"""
        item = _FakeItem("张三")
        self._wire(uia, monkeypatch, _FakePop(children=[item]))

        assert uia._pick_mention("张三") is True
        assert item.clicked == 1

    def test_unfiltered_single_contains_still_matches(self, uia, monkeypatch):
        """未过滤：唯一项归一化包含命中（含 U+2005）时正常点击。"""
        item = _FakeItem("安佳胤\u2005(家人)")
        self._wire(uia, monkeypatch, _FakePop(children=[item]))

        assert uia._pick_mention("安佳胤") is True
        assert item.clicked == 1

    def test_filtered_single_related_item_is_selected(self, uia, monkeypatch):
        """已过滤：唯一项与目标共享字符（但不含全名）→ 兜底命中。"""
        item = _FakeItem("安小宇")
        self._wire(uia, monkeypatch, _FakePop(children=[item]))

        assert uia._pick_mention("安佳胤", timeout=1.5,
                                 allow_single=True) is True
        assert item.clicked == 1

    def test_filtered_single_unrelated_item_is_rejected(
            self, uia, monkeypatch):
        """已过滤但唯一项完全不符 → fail-closed，绝不猜一个 @ 上去。"""
        item = _FakeItem("张三")
        self._wire(uia, monkeypatch, _FakePop(children=[item]))

        assert uia._pick_mention("安佳胤", timeout=1.5,
                                 allow_single=True) is False
        assert item.clicked == 0

    def test_multiple_items_never_use_single_fallback(
            self, uia, monkeypatch):
        """多候选时唯一项兜底不生效：即使 allow_single=True 也不误选。"""
        items = [_FakeItem("安小宇"), _FakeItem("安小鹏")]
        self._wire(uia, monkeypatch, _FakePop(children=items))

        assert uia._pick_mention("安佳胤", allow_single=True) is False
        assert [i.clicked for i in items] == [0, 0]


class TestMatchMentionItemDirect:
    """_match_mention_item 直接单测：默认 False / allow_single=True 两条路径。"""

    def test_default_denies_unrelated_single(self):
        item = _FakeItem("张三")
        assert WeChatUIA._match_mention_item([item], "安佳胤") is None

    def test_default_denies_related_single_without_exact_or_contains(self):
        """默认路径：共享字符也不够，没有精确/包含命中就不兜底。"""
        item = _FakeItem("安小宇")
        assert WeChatUIA._match_mention_item([item], "安佳胤") is None

    def test_allow_single_accepts_related_item(self):
        item = _FakeItem("安小宇")
        assert WeChatUIA._match_mention_item(
            [item], "安佳胤", allow_single=True) is item

    def test_allow_single_rejects_unrelated_item(self):
        item = _FakeItem("张三")
        assert WeChatUIA._match_mention_item(
            [item], "安佳胤", allow_single=True) is None

    def test_allow_single_multi_items_still_reject(self):
        items = [_FakeItem("安小宇"), _FakeItem("安小鹏")]
        assert WeChatUIA._match_mention_item(
            items, "安佳胤", allow_single=True) is None

    def test_exact_and_contains_precede_guard(self):
        """精确/包含命中优先于兜底，allow_single=False 下也照常命中。"""
        exact = _FakeItem("安佳胤")
        assert WeChatUIA._match_mention_item([exact], "安佳胤") is exact
        contained = _FakeItem("安佳胤\u2005(家人)")
        assert WeChatUIA._match_mention_item([contained], "安佳胤") is contained

    def test_target_normalized_for_direct_call(self):
        """直接调用传入带 U+2005 的 target 也归一化后再匹配（幂等）。"""
        item = _FakeItem("安佳胤")
        assert WeChatUIA._match_mention_item([item], "安佳胤\u2005") is item


class _FakeEdit:
    """输入行替身：只记录按键，供 at_members 两阶段验证使用。"""

    def __init__(self):
        self.keys = []
        self.clicks = 0

    def SendKeys(self, keys, waitTime=0):
        self.keys.append(keys)

    def Click(self):
        self.clicks += 1


class TestAtMembersTwoStage:
    """锁死 at_members 两阶段调用的 allow_single 可信度区分。"""

    def test_first_unfiltered_then_filtered_allow_single(
            self, uia, monkeypatch):
        """首次未过滤用默认 False；回退（已按名字过滤）传 allow_single=True。"""
        edit = _FakeEdit()
        calls = []

        def fake_pick(name, timeout=2.0, allow_single=False):
            calls.append((name, timeout, allow_single))
            return len(calls) == 2  # 首次失败，回退成功

        monkeypatch.setattr(uia, "ensure_window", lambda: True)
        monkeypatch.setattr(uia, "same_chat", lambda a, b: True)
        monkeypatch.setattr(uia, "current_chat", lambda: "家人群")
        monkeypatch.setattr(uia, "_chat_input", lambda: edit)
        monkeypatch.setattr(uia, "_paste_append", lambda e, t: True)
        monkeypatch.setattr(uia, "_pick_mention", fake_pick)

        assert uia.at_members(["安佳胤"], "收到", who="家人群") is True
        assert calls == [("安佳胤", 2.0, False), ("安佳胤", 1.5, True)]
        assert edit.keys.count("@") == 1
        assert edit.keys.count("安佳胤") == 1      # 仅回退时敲入一次名字
        assert edit.keys[-1] == "{Enter}"          # 只回车一次
