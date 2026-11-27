# -*- coding: utf-8 -*-
"""双开微信：监控账号 ↔ 实例绑定与发送定向（不依赖真实微信）。"""
from __future__ import annotations

import json
import threading
import time

import pytest

from app.config import KEY_RUNTIME_WECHAT_ACCOUNT, KEY_RUNTIME_WECHAT_ACCOUNT_NICKS
from app.db import Database
from app.notifier import Notifier
from app.sender import Sender
from app.wechat_service import WeChatService
from wechatauto import db as wxdb_mod


# ---------------------------------------------------------------------------
# WeChatDB.find_account_pid：strict 单实例校验 / 多实例归属
# ---------------------------------------------------------------------------
def _fake_wxdb(account: str = "wxid_self_abcd"):
    g = object.__new__(wxdb_mod.WeChatDB)
    g.account = account
    return g


def test_strict_single_rejects_other_account(monkeypatch):
    g = _fake_wxdb()
    monkeypatch.setattr(wxdb_mod, "extract_master_key_from_cfg",
                        lambda pid: ("00" * 32, 0, "wxid_other"))
    g._master_matches_account = lambda master: False
    assert g.find_account_pid([111], strict=True) is None


def test_strict_single_accepts_matching_account(monkeypatch):
    g = _fake_wxdb()
    monkeypatch.setattr(wxdb_mod, "extract_master_key_from_cfg",
                        lambda pid: ("00" * 32, 0, "wxid_self"))
    g._master_matches_account = lambda master: False
    assert g.find_account_pid([111], strict=True) == 111


def test_strict_single_key_match_covers_custom_dir(monkeypatch):
    """账号目录名不是登录 wxid（自定义微信号）时靠主密钥强校验。"""
    g = _fake_wxdb("my_shop_abcd")
    monkeypatch.setattr(wxdb_mod, "extract_master_key_from_cfg",
                        lambda pid: ("11" * 32, 0, "wxid_login"))
    g._master_matches_account = lambda master: True
    assert g.find_account_pid([222], strict=True) == 222


def test_strict_single_unverifiable_keeps_pid(monkeypatch):
    """读不到进程内存时无法否定，单实例不阻断（保持可用）。"""
    g = _fake_wxdb()
    monkeypatch.setattr(wxdb_mod, "extract_master_key_from_cfg",
                        lambda pid: None)
    assert g.find_account_pid([333], strict=True) == 333


def test_multi_instance_maps_by_wxid_then_master(monkeypatch):
    g = _fake_wxdb()

    def fake_extract(pid):
        if pid == 1:
            return ("22" * 32, 0, "wxid_other")
        return ("33" * 32, 0, "wxid_self")

    monkeypatch.setattr(wxdb_mod, "extract_master_key_from_cfg", fake_extract)
    g._master_matches_account = lambda master: False
    assert g.find_account_pid([1, 2]) == 2


def test_multi_instance_unmapped_returns_none(monkeypatch):
    g = _fake_wxdb()
    monkeypatch.setattr(wxdb_mod, "extract_master_key_from_cfg",
                        lambda pid: ("44" * 32, 0, f"wxid_other{pid}"))
    g._master_matches_account = lambda master: False
    assert g.find_account_pid([1, 2]) is None


# ---------------------------------------------------------------------------
# 打开文件句柄信号：微信新版本 cfg 解析失效时仍能确认账号归属
# ---------------------------------------------------------------------------
def test_multi_instance_maps_by_account_handles(monkeypatch):
    g = _fake_wxdb()
    g._pid_account_match = lambda pid: {1: False, 2: True}.get(pid)
    # cfg 解析全部失效（用户真机 4.x 实测），句柄仍能确认
    monkeypatch.setattr(wxdb_mod, "extract_master_key_from_cfg",
                        lambda pid: None)
    assert g.find_account_pid([1, 2]) == 2


def test_multi_instance_all_other_accounts_returns_none(monkeypatch):
    """句柄已明确显示两个实例登录的都是别的账号：fail-closed。"""
    g = _fake_wxdb()
    g._pid_account_match = lambda pid: False
    monkeypatch.setattr(wxdb_mod, "extract_master_key_from_cfg",
                        lambda pid: ("55" * 32, 0, "wxid_self"))
    assert g.find_account_pid([1, 2]) is None


def test_single_strict_handle_match_skips_process_scan(monkeypatch):
    g = _fake_wxdb()

    def _boom(pid):
        raise AssertionError("句柄已确认，不应再读进程内存")

    g._pid_account_match = lambda pid: True
    monkeypatch.setattr(wxdb_mod, "extract_master_key_from_cfg", _boom)
    assert g.find_account_pid([111], strict=True) == 111


def test_single_strict_other_account_handles_rejected():
    g = _fake_wxdb()
    g._pid_account_match = lambda pid: False
    assert g.find_account_pid([111], strict=True) is None


def test_single_auto_mode_returns_without_handle_probe():
    """自动模式单实例不探测句柄（保持零额外开销）。"""
    g = _fake_wxdb()
    calls = []
    g._pid_account_match = lambda pid: calls.append(pid) or True
    assert g.find_account_pid([111]) == 111
    assert calls == []


class _OpenFile:
    def __init__(self, path: str):
        self.path = path


class TestPidAccountMatch:
    """open_files 句柄归属：不依赖微信版本内存布局。"""

    def _db(self, tmp_path):
        g = object.__new__(wxdb_mod.WeChatDB)
        g.db_dir = str(tmp_path)
        g.account = "wxid_self_abcd"
        g.account_dir = str(tmp_path / "wxid_self_abcd")
        return g

    def _patch_files(self, monkeypatch, paths):
        import psutil

        class _Proc:
            def __init__(self, pid):
                self.pid = pid

            def open_files(self):
                return [_OpenFile(p) for p in paths]

        monkeypatch.setattr(psutil, "Process", _Proc)

    def test_account_dir_handle_is_match(self, tmp_path, monkeypatch):
        g = self._db(tmp_path)
        self._patch_files(monkeypatch, [
            str(tmp_path / "wxid_self_abcd" / "db_storage" / "contact.db")])
        assert g._pid_account_match(1) is True

    def test_other_account_only_is_rejected(self, tmp_path, monkeypatch):
        g = self._db(tmp_path)
        self._patch_files(monkeypatch, [
            str(tmp_path / "wxid_other_efgh" / "db_storage" / "contact.db")])
        assert g._pid_account_match(1) is False

    def test_unrelated_files_unknown(self, tmp_path, monkeypatch):
        g = self._db(tmp_path)
        self._patch_files(monkeypatch, [r"C:\Windows\Fonts\msyh.ttc"])
        assert g._pid_account_match(1) is None

    def test_no_handles_unknown(self, tmp_path, monkeypatch):
        g = self._db(tmp_path)
        self._patch_files(monkeypatch, [])
        assert g._pid_account_match(1) is None

    def test_access_error_unknown(self, tmp_path, monkeypatch):
        import psutil

        class _Proc:
            def __init__(self, pid):
                pass

            def open_files(self):
                raise psutil.AccessDenied(pid=1)

        monkeypatch.setattr(psutil, "Process", _Proc)
        g = self._db(tmp_path)
        assert g._pid_account_match(1) is None

    def test_method_delegates_to_module_function(self, tmp_path, monkeypatch):
        """服务层（无 WeChatDB 实例）也走同一套句柄判定。"""
        calls = []

        def _spy(*a, **kw):
            calls.append((a, kw))
            return True

        monkeypatch.setattr(wxdb_mod, "pid_account_match", _spy)
        g = self._db(tmp_path)
        assert g._pid_account_match(7) is True
        assert calls == [((7, str(tmp_path)),
                          {"account": "wxid_self_abcd",
                           "account_dir": str(tmp_path / "wxid_self_abcd")})]


# ---------------------------------------------------------------------------
# WeChatService._account_instance_absent：切号前的快速失败判定
# ---------------------------------------------------------------------------
class TestAccountInstanceAbsent:
    def test_all_instances_other_account(self, monkeypatch):
        monkeypatch.setattr(wxdb_mod, "enumerate_weixin_pids", lambda: [1, 2])
        monkeypatch.setattr(wxdb_mod, "pid_account_match", lambda *a: False)
        assert WeChatService._account_instance_absent(
            "wxid_a_abcd", r"E:\wx") is True

    def test_any_match_does_not_block(self, monkeypatch):
        monkeypatch.setattr(wxdb_mod, "enumerate_weixin_pids", lambda: [1, 2])
        monkeypatch.setattr(wxdb_mod, "pid_account_match",
                            lambda pid, *a: pid == 2)
        assert WeChatService._account_instance_absent(
            "wxid_a_abcd", r"E:\wx") is False

    def test_unknown_evidence_does_not_block(self, monkeypatch):
        monkeypatch.setattr(wxdb_mod, "enumerate_weixin_pids", lambda: [1])
        monkeypatch.setattr(wxdb_mod, "pid_account_match", lambda *a: None)
        assert WeChatService._account_instance_absent(
            "wxid_a_abcd", r"E:\wx") is False

    def test_no_weixin_running_does_not_block(self, monkeypatch):
        monkeypatch.setattr(wxdb_mod, "enumerate_weixin_pids", lambda: [])
        assert WeChatService._account_instance_absent(
            "wxid_a_abcd", r"E:\wx") is False


# ---------------------------------------------------------------------------
# 密钥缓存：页 1 HMAC 校验失败必须从内存/磁盘移除（防止用错账号的密钥）
# ---------------------------------------------------------------------------
class TestStaleKeyPurge:
    def test_keys_failing_page_hmac_are_dropped(self, tmp_path):
        keys_file = tmp_path / "keys.json"
        keys_file.write_text('{"contact.db": "00112233"}', encoding="utf-8")
        db_file = tmp_path / "contact.db"
        db_file.write_bytes(b"\x00" * 4096)     # 页 1 与密钥 HMAC 不匹配
        g = object.__new__(wxdb_mod.WeChatDB)
        g._db_files = [("contact.db", str(db_file), 4096)]
        g._keys = {}
        g.keys_file = str(keys_file)
        g._key_works = lambda rel: False
        g.extract_keys = lambda: {}
        g.extract_master_key = lambda: None

        g._load_or_extract_keys()

        assert g._keys == {}
        assert json.loads(keys_file.read_text(encoding="utf-8")) == {}
        assert g.unkeyed == ["contact.db"]

    def test_unreadable_db_keeps_key(self, tmp_path):
        """源库读不到（临时占用/权限）无法判定密钥失效，不得误删。"""
        keys_file = tmp_path / "keys.json"
        keys_file.write_text('{"contact.db": "00112233"}', encoding="utf-8")
        g = object.__new__(wxdb_mod.WeChatDB)
        g._db_files = [("contact.db", str(tmp_path / "missing.db"), 4096)]
        g._keys = {}
        g.keys_file = str(keys_file)
        g._key_works = lambda rel: False
        g.extract_keys = lambda: {}
        g.extract_master_key = lambda: None

        g._load_or_extract_keys()

        assert "contact.db" in g._keys


# ---------------------------------------------------------------------------
# enumerate_weixin_pids：只统计主实例（1 主 + N 子不再误报「5 个微信」）
# ---------------------------------------------------------------------------
class _FakeProcInfo:
    def __init__(self, pid: int, name: str, ppid: int):
        self.info = {"pid": pid, "name": name, "ppid": ppid}


def _patch_process_tree(monkeypatch, procs, parent_names):
    import psutil

    monkeypatch.setattr(
        psutil, "process_iter", lambda attrs=None: iter(procs))

    class _Parent:
        def __init__(self, ppid):
            self.ppid = ppid

        def name(self):
            return parent_names.get(self.ppid, "unknown")

    monkeypatch.setattr(psutil, "Process", _Parent)


def test_enumerate_counts_only_main_instances(monkeypatch):
    """真机实测：1 个主进程 + 4 个渲染子进程 → 只算 1 个微信。"""
    procs = [
        _FakeProcInfo(11800, "Weixin.exe", 5096),
        _FakeProcInfo(12676, "Weixin.exe", 11800),
        _FakeProcInfo(13244, "weixin.exe", 11800),
        _FakeProcInfo(13248, "Weixin.exe", 11800),
        _FakeProcInfo(17784, "Weixin.exe", 11800),
    ]
    _patch_process_tree(monkeypatch, procs,
                        {5096: "explorer.exe", 11800: "Weixin.exe"})
    assert wxdb_mod.enumerate_weixin_pids() == [11800]


def test_enumerate_multi_open_counts_main_only(monkeypatch):
    """双开：两个主进程都保留，各自的子进程剔除。"""
    procs = [
        _FakeProcInfo(100, "Weixin.exe", 1),
        _FakeProcInfo(101, "Weixin.exe", 100),
        _FakeProcInfo(200, "Weixin.exe", 1),
        _FakeProcInfo(201, "Weixin.exe", 200),
    ]
    _patch_process_tree(monkeypatch, procs,
                        {1: "explorer.exe", 100: "Weixin.exe",
                         200: "Weixin.exe"})
    assert wxdb_mod.enumerate_weixin_pids() == [100, 200]


def test_enumerate_no_weixin(monkeypatch):
    _patch_process_tree(
        monkeypatch,
        [_FakeProcInfo(5, "explorer.exe", 1)], {1: "explorer.exe"})
    assert wxdb_mod.enumerate_weixin_pids() == []


def test_enumerate_all_children_falls_back_to_raw(monkeypatch):
    """极端情况全部被判成子进程时回退全量，宁可多报不误杀。"""
    procs = [
        _FakeProcInfo(100, "Weixin.exe", 100),
        _FakeProcInfo(101, "Weixin.exe", 100),
    ]
    _patch_process_tree(monkeypatch, procs, {100: "Weixin.exe"})
    assert wxdb_mod.enumerate_weixin_pids() == [100, 101]


def test_enumerate_all_keeps_children_for_key_scan(monkeypatch):
    """内存密钥扫描（AES/Config.Cipher）必须覆盖子进程，不能只看主进程。"""
    procs = [
        _FakeProcInfo(11800, "Weixin.exe", 5096),
        _FakeProcInfo(12676, "Weixin.exe", 11800),
        _FakeProcInfo(13244, "Weixin.exe", 11800),
    ]
    _patch_process_tree(monkeypatch, procs,
                        {5096: "explorer.exe", 11800: "Weixin.exe"})
    assert wxdb_mod.enumerate_all_weixin_pids() == [11800, 12676, 13244]
    g = object.__new__(wxdb_mod.WeChatDB)
    assert g._find_weixin_pids() == [11800, 12676, 13244]


# ---------------------------------------------------------------------------
# WeChatService.account_binding：状态回显与 fail-closed 原因
# ---------------------------------------------------------------------------
class _FakeSettingsDB:
    def __init__(self, pinned: str = ""):
        self.pinned = pinned
        self.settings: dict[str, str] = {}

    def get_setting(self, key, default=""):
        if key == KEY_RUNTIME_WECHAT_ACCOUNT:
            return self.pinned
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


class _FakeWxdb:
    def __init__(self, pid: int, matched: bool = True):
        self.account = "wxid_self_abcd"
        self.wxid = "wxid_self"
        self.pid = pid
        self.strict_called = None
        self.matched = matched

    def get_self_info(self):
        return {"nick_name": "测试号"}

    def find_account_pid(self, pids, strict=False):
        self.strict_called = strict
        return self.pid


def _fake_service(pinned: str = "", pid: int = 0):
    svc = object.__new__(WeChatService)
    svc._db = _FakeSettingsDB(pinned)
    svc._wxdb = _FakeWxdb(pid)
    svc._available = True
    return svc


def test_binding_multi_unmapped_is_explicit(monkeypatch):
    svc = _fake_service()
    monkeypatch.setattr(wxdb_mod, "enumerate_weixin_pids", lambda: [11, 22])
    info = svc._compute_account_binding()
    assert info["instance_count"] == 2
    assert info["mapped"] is False
    assert "多个微信实例" in info["reason"]
    assert info["wxid"] == "wxid_self"
    assert info["nick_name"] == "测试号"


def test_binding_pinned_uses_strict_mapping(monkeypatch):
    svc = _fake_service(pinned="wxid_self_abcd")
    monkeypatch.setattr(wxdb_mod, "enumerate_weixin_pids", lambda: [11])
    info = svc._compute_account_binding()
    assert svc._wxdb.strict_called is True
    assert info["pinned"] is True
    # 单实例但 strict 未匹配 → fail-closed 原因
    assert info["mapped"] is False
    assert "不是所选监控账号" in info["reason"]


# ---------------------------------------------------------------------------
# 账号昵称：读到的昵称落库，设置页选择账号时显示名称而非 wxid
# ---------------------------------------------------------------------------
def test_binding_persists_discovered_nick_name(monkeypatch):
    svc = _fake_service(pinned="wxid_self_abcd")
    monkeypatch.setattr(wxdb_mod, "enumerate_weixin_pids", lambda: [11])
    svc._compute_account_binding()
    raw = svc._db.get_setting(KEY_RUNTIME_WECHAT_ACCOUNT_NICKS, "")
    assert "测试号" in raw


def test_binding_nick_falls_back_to_saved_map_without_db(monkeypatch):
    """降级（未打开库）时用已保存昵称，顶栏不退回 wxid。"""
    svc = object.__new__(WeChatService)
    svc._db = _FakeSettingsDB("wxid_bpn12gyyxdpw11_738a")
    svc._db.settings[KEY_RUNTIME_WECHAT_ACCOUNT_NICKS] = json.dumps(
        {"wxid_bpn12gyyxdpw11_738a": "kun"}, ensure_ascii=False)
    svc._wxdb = None
    svc._available = False
    svc._binding_lock = threading.Lock()
    svc._binding_cache = None
    monkeypatch.setattr(wxdb_mod, "enumerate_weixin_pids", lambda: [])
    info = svc._compute_account_binding()
    assert info["nick_name"] == "kun"
    assert info["account"] == "wxid_bpn12gyyxdpw11_738a"
    assert info["mapped"] is False


def test_account_list_shows_saved_nick_names(monkeypatch):
    svc = _fake_service(pinned="wxid_self_abcd")
    svc._wxdb.account = "wxid_self_abcd"
    svc._binding_lock = threading.Lock()
    svc._binding_cache = None
    svc._db.settings[KEY_RUNTIME_WECHAT_ACCOUNT_NICKS] = json.dumps(
        {"wxid_self_abcd": "安佳佩"}, ensure_ascii=False)
    monkeypatch.setattr(wxdb_mod, "list_accounts", lambda: [
        {"account": "wxid_self_abcd", "wxid": "wxid_self",
         "last_activity": 2.0},
        {"account": "wxid_old_efgh", "wxid": "wxid_old",
         "last_activity": 1.0},
    ])
    items = {i["account"]: i for i in svc.account_list()}
    assert items["wxid_self_abcd"]["nick_name"] == "安佳佩"
    assert items["wxid_self_abcd"]["active"] is True
    assert items["wxid_self_abcd"]["configured"] is True
    assert items["wxid_old_efgh"]["nick_name"] == ""


def test_account_list_falls_back_to_cached_binding_nick(monkeypatch):
    """昵称尚未落库时用最近一次绑定结果补上（设置页立即能认人）。"""
    svc = _fake_service(pinned="")
    svc._wxdb.account = "wxid_self_abcd"
    svc._binding_lock = threading.Lock()
    svc._binding_cache = (time.time(), {
        "account": "wxid_self_abcd", "nick_name": "测试号"})
    monkeypatch.setattr(wxdb_mod, "list_accounts", lambda: [
        {"account": "wxid_self_abcd", "wxid": "wxid_self",
         "last_activity": 1.0},
    ])
    assert svc.account_list()[0]["nick_name"] == "测试号"


# ---------------------------------------------------------------------------
# Sender._ensure_gui：按绑定 PID 创建 / 切换账号重建 / 无法确认就拒发
# ---------------------------------------------------------------------------
def _sender_with(binding):
    s = Sender(Database(":memory:"), Notifier())
    s.set_wechat_binding_provider(lambda: binding)
    return s


def test_sender_refuses_when_multi_instance_unmapped():
    s = _sender_with({"account": "a", "pid": 0, "instance_count": 2,
                      "mapped": False, "pinned": False,
                      "reason": "检测到多个微信实例，无法确认"})
    assert s._ensure_gui() is None
    assert "多个微信实例" in s._last_gui_error


def test_sender_refuses_pinned_mismatch_single_instance():
    s = _sender_with({"account": "a", "pid": 0, "instance_count": 1,
                      "mapped": False, "pinned": True,
                      "reason": "当前登录的微信不是所选监控账号"})
    assert s._ensure_gui() is None
    assert "不是所选监控账号" in s._last_gui_error


def test_sender_creates_gui_with_target_pid(monkeypatch):
    import wechatauto.guia as guia_mod

    created = []

    class FakeGUI:
        def __init__(self, pid=None):
            self.pid = pid
            created.append(self)

        def is_alive(self):
            return True

    monkeypatch.setattr(guia_mod, "WeChatGUI", FakeGUI)
    binding = {"account": "wxid_self_abcd", "pid": 4242,
               "instance_count": 2, "mapped": True, "pinned": True,
               "reason": ""}
    s = _sender_with(binding)
    gui = s._ensure_gui()
    assert gui is created[0]
    assert gui.pid == 4242
    assert s._gui_account == "wxid_self_abcd"


def test_sender_rebuilds_gui_after_account_switch(monkeypatch):
    import wechatauto.guia as guia_mod

    created = []

    class FakeGUI:
        def __init__(self, pid=None):
            self.pid = pid
            created.append(self)

        def is_alive(self):
            return True

    monkeypatch.setattr(guia_mod, "WeChatGUI", FakeGUI)
    state = {"account": "wxid_a_abcd", "pid": 111, "instance_count": 2,
             "mapped": True, "pinned": True, "reason": ""}
    s = _sender_with(None)
    s.set_wechat_binding_provider(lambda: dict(state))
    first = s._ensure_gui()
    state.update(account="wxid_b_abcd", pid=222)
    second = s._ensure_gui()
    assert first.pid == 111 and second.pid == 222
    assert second is not first
    assert len(created) == 2


# ---------------------------------------------------------------------------
# 切换账号后：旧账号的排队回复不得用新账号发出
# ---------------------------------------------------------------------------
def test_submit_stamps_current_account():
    from app.sender import SendTask

    s = _sender_with({"account": "wxid_a_abcd", "pid": 111,
                      "instance_count": 2, "mapped": True,
                      "pinned": True, "reason": ""})
    task = SendTask(opportunity_id=1, session_username="u",
                    session_name="U", text="hi")
    s.submit(task)
    assert task.account == "wxid_a_abcd"
    # 队列里取出同一任务（无发送线程在消费）
    assert s._queue.get_nowait() is task


def test_sender_cancels_task_from_previous_account(monkeypatch):
    from app.sender import SendTask, _Stages

    s = _sender_with({"account": "wxid_b_abcd", "pid": 222,
                      "instance_count": 2, "mapped": True,
                      "pinned": True, "reason": ""})

    def _no_gui(*args, **kwargs):
        raise AssertionError("切换账号后的旧任务不得触碰微信窗口")

    monkeypatch.setattr(s, "_ensure_gui", _no_gui)
    failures = []
    monkeypatch.setattr(
        s, "_fail",
        lambda task, message, stage=None: failures.append(message))
    task = SendTask(opportunity_id=2, session_username="u",
                    session_name="U", text="hi",
                    account="wxid_a_abcd")
    s._process_locked(task, _Stages())
    assert len(failures) == 1
    assert "wxid_a_abcd" in failures[0]
    assert "已取消发送" in failures[0]


def test_sender_keeps_task_for_current_account(monkeypatch):
    from app.sender import SendTask, _Stages

    s = _sender_with({"account": "wxid_a_abcd", "pid": 111,
                      "instance_count": 2, "mapped": True,
                      "pinned": True, "reason": ""})
    called = []
    monkeypatch.setattr(s, "_ensure_gui", lambda *a, **k: called.append(1) or None)
    s._last_gui_error = "窗口未就绪"
    failures = []
    monkeypatch.setattr(
        s, "_fail",
        lambda task, message, stage=None: failures.append(message))
    task = SendTask(opportunity_id=3, session_username="u",
                    session_name="U", text="hi",
                    account="wxid_a_abcd")
    s._process_locked(task, _Stages())
    assert called          # 账号一致：继续走正常发送流程
    assert failures == ["窗口未就绪"]