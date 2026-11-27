# -*- coding: utf-8 -*-
"""微信数据库解密缓存目录（可配置）专项测试。

背景：解密后的 SQLCipher 副本原来固定落在 ``%TEMP%/wechatauto_db/<account>``，
系统磁盘清理 / 存储感知会把它删掉，下次启动就要全量重新解密，甚至要重新从
微信进程内存提取密钥。本文件锁定：

1. 默认目录在客服系统运行目录下（``data/wechat_cache``），与 %TEMP% 无关；
2. 相对路径按 kefu-system/ 解析，``~`` 与环境变量可展开（不受 cwd 影响）；
3. 保存前做可写性校验：指向文件 / 无法创建 / 不可写都拒绝；
4. ``GET|PUT /api/system/cache`` 契约（空值 = 恢复默认）；
5. 换目录时旧 %TEMP% 下的 keys.json 会自动复制过去（只在目标缺失时）。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config as config_mod
from app.aggregator import Aggregator
from app.api import ALL_ROUTERS
from app.api import deps
from app.db import Database
from app.notifier import Notifier
from app import wechat_service as ws_mod
from app.wechat_service import WeChatService
from wechatauto import db as wxdb_mod


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------
class TestResolveCacheDir:
    def test_default_is_under_project_data_dir(self):
        path = config_mod.resolve_cache_dir("")
        assert path == config_mod.CACHE_DIR_DEFAULT
        # 关键诉求：不再落在 %TEMP%（可能被系统清理 / 可能在 C 盘）
        assert str(path).startswith(str(config_mod.BASE_DIR))
        assert "wechatauto_db" not in str(path)
        assert path.name == "wechat_cache"

    def test_default_is_data_dir_child(self):
        assert config_mod.CACHE_DIR_DEFAULT.parent == config_mod.DATA_DIR

    def test_blank_variants_fall_back_to_default(self):
        for raw in (None, "", "   ", "\t"):
            assert config_mod.resolve_cache_dir(raw) == config_mod.CACHE_DIR_DEFAULT

    def test_relative_path_resolves_under_base_dir(self):
        path = config_mod.resolve_cache_dir("my_cache/win")
        assert path.is_absolute()
        assert path == (config_mod.BASE_DIR / "my_cache" / "win").resolve()

    def test_absolute_path_kept(self, tmp_path: Path):
        target = tmp_path / "wx_cache"
        assert config_mod.resolve_cache_dir(str(target)) == target.resolve()

    def test_tilde_expanded(self):
        path = config_mod.resolve_cache_dir("~/kefu_cache")
        assert "~" not in str(path)
        assert path.is_absolute()

    def test_env_var_expanded(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("KEFU_TEST_CACHE", str(tmp_path))
        path = config_mod.resolve_cache_dir("%KEFU_TEST_CACHE%/sub"
                                            if os.name == "nt"
                                            else "$KEFU_TEST_CACHE/sub")
        assert path == (tmp_path / "sub").resolve()


# ---------------------------------------------------------------------------
# 保存前校验
# ---------------------------------------------------------------------------
class TestCheckCacheDir:
    def test_creates_missing_directory(self, tmp_path: Path):
        target = tmp_path / "a" / "b" / "cache"
        ok, reason, path = config_mod.check_cache_dir(str(target))
        assert ok is True, reason
        assert path == target.resolve()
        assert path.is_dir()

    def test_empty_path_creates_default(self, monkeypatch, tmp_path: Path):
        default = tmp_path / "default_cache"
        monkeypatch.setattr(config_mod, "CACHE_DIR_DEFAULT", default)
        ok, reason, path = config_mod.check_cache_dir("")
        assert ok is True, reason
        assert path == default
        assert default.is_dir()

    def test_rejects_existing_file(self, tmp_path: Path):
        file_path = tmp_path / "not_a_dir.txt"
        file_path.write_text("x", encoding="utf-8")
        ok, reason, _ = config_mod.check_cache_dir(str(file_path))
        assert ok is False
        assert "不是文件夹" in reason

    def test_rejects_too_long_path(self):
        ok, reason, _ = config_mod.check_cache_dir("x" * 260)
        assert ok is False
        assert "路径过长" in reason

    def test_rejects_when_mkdir_fails(self, monkeypatch, tmp_path: Path):
        def boom(self, *a, **kw):
            raise OSError("拒绝访问")

        monkeypatch.setattr(Path, "mkdir", boom)
        ok, reason, _ = config_mod.check_cache_dir(str(tmp_path / "x"))
        assert ok is False
        assert "无法创建目录" in reason

    def test_rejects_when_dir_not_writable(self, monkeypatch, tmp_path: Path):
        def boom(self, data):
            raise OSError("磁盘只读")

        monkeypatch.setattr(Path, "write_bytes", boom)
        ok, reason, _ = config_mod.check_cache_dir(str(tmp_path / "ro"))
        assert ok is False
        assert "不可写" in reason


# ---------------------------------------------------------------------------
# 占用统计
# ---------------------------------------------------------------------------
class TestDirUsage:
    def test_counts_files_and_bytes(self, tmp_path: Path):
        (tmp_path / "a.db").write_bytes(b"x" * 100)
        (tmp_path / "b.stamp").write_bytes(b"y" * 23)
        # 解密缓存现在按账号隔离在子目录，统计必须递归
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "c.db").write_bytes(b"z" * 7)
        size, count = config_mod.dir_usage(tmp_path)
        assert size == 130
        assert count == 3

    def test_missing_dir_returns_zero(self, tmp_path: Path):
        assert config_mod.dir_usage(tmp_path / "nope") == (0, 0)


# ---------------------------------------------------------------------------
# 服务层：配置读取 / 旧密钥迁移 / 目录信息
# ---------------------------------------------------------------------------
def _make_service(tmp_path: Path) -> tuple[WeChatService, Database]:
    db = Database(str(tmp_path / "svc.sqlite3"))
    svc = WeChatService(db, Aggregator(lambda unit: None))
    return svc, db


class TestServiceCacheDir:
    def test_default_when_unset(self, monkeypatch, tmp_path: Path):
        default = tmp_path / "cache"
        # resolve_cache_dir 在 config 模块内读取该常量；cache_dir_info 直接
        # 引用 wechat_service 里 import 的那份，两处都要打补丁
        monkeypatch.setattr(config_mod, "CACHE_DIR_DEFAULT", default)
        monkeypatch.setattr(ws_mod, "CACHE_DIR_DEFAULT", default)
        svc, db = _make_service(tmp_path)
        try:
            assert svc.configured_cache_dir() == default
        finally:
            db.close()

    def test_custom_setting_used(self, tmp_path: Path):
        svc, db = _make_service(tmp_path)
        target = tmp_path / "custom_cache"
        try:
            db.set_setting(config_mod.KEY_RUNTIME_CACHE_DIR, str(target))
            assert svc.configured_cache_dir() == target.resolve()
        finally:
            db.close()

    def test_cache_dir_info_contract(self, tmp_path: Path):
        svc, db = _make_service(tmp_path)
        try:
            target = tmp_path / "custom_cache"
            target.mkdir()
            (target / "message__message_0.db").write_bytes(b"z" * 64)
            db.set_setting(config_mod.KEY_RUNTIME_CACHE_DIR, str(target))
            info = svc.cache_dir_info()
            assert info["dir"] == str(target.resolve())
            assert info["configured"] == str(target)
            assert info["is_default"] is False
            assert info["size_bytes"] == 64
            assert info["file_count"] == 1
            # 未启动监听 → active False 且没有实际使用目录
            assert info["active"] is False
            assert info["active_dir"] == ""
            assert isinstance(info["legacy_dirs"], list)
        finally:
            db.close()

    def test_adopt_legacy_keys_copies_once(self, monkeypatch, tmp_path: Path):
        legacy_root = tmp_path / "legacy"
        account = legacy_root / "wxid_test_ab12"
        account.mkdir(parents=True)
        (account / "keys.json").write_text('{"a": "00"}', encoding="utf-8")
        (account / "image_keys.json").write_text('{"img": "11"}', encoding="utf-8")
        monkeypatch.setattr(ws_mod, "LEGACY_CACHE_ROOT", legacy_root)

        svc, db = _make_service(tmp_path)
        try:
            target = tmp_path / "new_cache"
            svc._adopt_legacy_keys(target)
            assert (target / "keys.json").read_text(encoding="utf-8") == '{"a": "00"}'
            assert (target / "image_keys.json").exists()

            # 已存在时绝不被旧目录覆盖（新目录可能已是当前账号的密钥）
            (target / "keys.json").write_text('{"new": "22"}', encoding="utf-8")
            svc._adopt_legacy_keys(target)
            assert (target / "keys.json").read_text(encoding="utf-8") == '{"new": "22"}'
        finally:
            db.close()

    def test_adopt_legacy_keys_without_legacy(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(ws_mod, "LEGACY_CACHE_ROOT", tmp_path / "empty")
        svc, db = _make_service(tmp_path)
        try:
            target = tmp_path / "new_cache"
            svc._adopt_legacy_keys(target)      # 不抛异常即可
            assert target.is_dir()
            assert not (target / "keys.json").exists()
        finally:
            db.close()

    def test_adopt_legacy_keys_picks_newest(self, monkeypatch, tmp_path: Path):
        legacy_root = tmp_path / "legacy"
        old = legacy_root / "old_account"
        new = legacy_root / "new_account"
        old.mkdir(parents=True)
        new.mkdir(parents=True)
        (old / "keys.json").write_text('{"v": "old"}', encoding="utf-8")
        (new / "keys.json").write_text('{"v": "new"}', encoding="utf-8")
        os.utime(old / "keys.json", (1_600_000_000, 1_600_000_000))
        os.utime(new / "keys.json", (1_700_000_000, 1_700_000_000))
        monkeypatch.setattr(ws_mod, "LEGACY_CACHE_ROOT", legacy_root)

        svc, db = _make_service(tmp_path)
        try:
            target = tmp_path / "new_cache"
            svc._adopt_legacy_keys(target)
            assert (target / "keys.json").read_text(encoding="utf-8") == '{"v": "new"}'
        finally:
            db.close()

    def test_adopt_prefers_shared_base_over_temp(self, monkeypatch, tmp_path):
        """升级到按账号隔离时，共享缓存根（升级前的 workdir）优先于 %TEMP%。"""
        base = tmp_path / "shared_cache"
        base.mkdir()
        (base / "keys.json").write_text('{"v": "base"}', encoding="utf-8")
        os.utime(base / "keys.json", (1_600_000_000, 1_600_000_000))
        legacy_root = tmp_path / "legacy"
        temp_acc = legacy_root / "wxid_self_abcd"
        temp_acc.mkdir(parents=True)
        (temp_acc / "keys.json").write_text('{"v": "temp"}', encoding="utf-8")
        os.utime(temp_acc / "keys.json", (1_800_000_000, 1_800_000_000))
        monkeypatch.setattr(ws_mod, "LEGACY_CACHE_ROOT", legacy_root)

        svc, db = _make_service(tmp_path)
        try:
            target = base / "wxid_self_abcd"
            svc._adopt_legacy_keys(target, base)
            assert (target / "keys.json").read_text(
                encoding="utf-8") == '{"v": "base"}'
        finally:
            db.close()

    def test_adopt_prefers_same_name_legacy_account(self, monkeypatch,
                                                    tmp_path: Path):
        """%TEMP% 下同名账号目录优先于其它账号（即使其它账号更新）。"""
        legacy_root = tmp_path / "legacy"
        same = legacy_root / "wxid_self_abcd"
        other = legacy_root / "wxid_other_efgh"
        same.mkdir(parents=True)
        other.mkdir(parents=True)
        (same / "keys.json").write_text('{"v": "same"}', encoding="utf-8")
        (other / "keys.json").write_text('{"v": "other"}', encoding="utf-8")
        os.utime(same / "keys.json", (1_600_000_000, 1_600_000_000))
        os.utime(other / "keys.json", (1_800_000_000, 1_800_000_000))
        monkeypatch.setattr(ws_mod, "LEGACY_CACHE_ROOT", legacy_root)

        svc, db = _make_service(tmp_path)
        try:
            target = tmp_path / "cache" / "wxid_self_abcd"
            svc._adopt_legacy_keys(target, tmp_path / "cache")
            assert (target / "keys.json").read_text(
                encoding="utf-8") == '{"v": "same"}'
        finally:
            db.close()


# ---------------------------------------------------------------------------
# 解密缓存/密钥按账号隔离：换号后不得复用上一个账号的解密结果
# ---------------------------------------------------------------------------
class TestAccountScopedCache:
    """``_open_listener_locked`` 必须用 <缓存根>/<账号> 作为 workdir。

    背景（真机）：keys.json/.pages/.stamp 只按库相对路径命名，共用目录时
    切到未登录的账号仍会返回原账号的联系人/群列表。
    """

    def _patch_wx(self, monkeypatch, db_root: Path):
        created = []

        class _FakeWxdb:
            def __init__(self, workdir="", account=None):
                self.workdir = str(workdir)
                self.account = account or ""
                self.pid = 0
                created.append(self)

            def get_messages(self, username, limit=1):
                return []

        class _FakeListener:
            def __init__(self, wxdb, interval=1.0, watermark=None):
                self.wxdb = wxdb
                self.watermark = dict(watermark or {})

            def add_listener(self, username, cb):
                pass

            def start(self):
                pass

            def stop(self):
                pass

        monkeypatch.setattr(wxdb_mod, "WeChatDB", _FakeWxdb)
        monkeypatch.setattr(wxdb_mod, "Listener", _FakeListener)
        monkeypatch.setattr(wxdb_mod, "auto_detect_db_dir",
                            lambda: str(db_root))
        return created

    def test_explicit_account_isolated_subdir(self, monkeypatch,
                                              tmp_path: Path):
        db_root = tmp_path / "wx_files"
        (db_root / "wxid_self_abcd" / "db_storage").mkdir(parents=True)
        cache = tmp_path / "cache"
        svc, db = _make_service(tmp_path)
        db.set_setting(config_mod.KEY_RUNTIME_CACHE_DIR, str(cache))
        created = self._patch_wx(monkeypatch, db_root)
        adopted = []
        monkeypatch.setattr(
            svc, "_adopt_legacy_keys",
            lambda target, base=None: adopted.append((target, base)))
        monkeypatch.setattr(wxdb_mod, "enumerate_weixin_pids", lambda: [])
        try:
            with svc._listener_lock:
                ok = svc._open_listener_locked("wxid_self_abcd")
            assert ok is True
            assert created[0].workdir == str(cache / "wxid_self_abcd")
            assert created[0].account == "wxid_self_abcd"
            assert adopted == [(cache / "wxid_self_abcd", cache)]
        finally:
            svc.stop()
            db.close()

    def test_auto_account_uses_resolved_subdir(self, monkeypatch,
                                               tmp_path: Path):
        db_root = tmp_path / "wx_files"
        db_root.mkdir()
        cache = tmp_path / "cache"
        svc, db = _make_service(tmp_path)
        db.set_setting(config_mod.KEY_RUNTIME_CACHE_DIR, str(cache))
        created = self._patch_wx(monkeypatch, db_root)
        monkeypatch.setattr(wxdb_mod, "list_accounts", lambda: [
            {"account": "wxid_self_abcd", "wxid": "wxid_self"}])
        monkeypatch.setattr(svc, "_adopt_legacy_keys", lambda *a, **k: None)
        try:
            with svc._listener_lock:
                ok = svc._open_listener_locked("")
            assert ok is True
            assert created[0].workdir == str(cache / "wxid_self_abcd")
            assert created[0].account == ""
        finally:
            svc.stop()
            db.close()

    def test_not_running_account_fails_fast(self, monkeypatch,
                                            tmp_path: Path):
        """句柄明确显示运行中的微信都属于别的账号 → 立即降级，不构造库。"""
        db_root = tmp_path / "wx_files"
        (db_root / "wxid_self_abcd" / "db_storage").mkdir(parents=True)
        svc, db = _make_service(tmp_path)
        constructed = []
        monkeypatch.setattr(wxdb_mod, "WeChatDB",
                            lambda **kw: constructed.append(kw))
        monkeypatch.setattr(wxdb_mod, "auto_detect_db_dir",
                            lambda: str(db_root))
        monkeypatch.setattr(wxdb_mod, "enumerate_weixin_pids", lambda: [11800])
        monkeypatch.setattr(wxdb_mod, "pid_account_match",
                            lambda *a: False)
        try:
            with svc._listener_lock:
                ok = svc._open_listener_locked("wxid_self_abcd")
            assert ok is False
            assert constructed == []
            assert svc._wxdb is None
            assert "当前未登录" in svc._reason
        finally:
            db.close()

    def test_unknown_instance_evidence_still_opens(self, monkeypatch,
                                                   tmp_path: Path):
        """句柄读不到（权限受限）时不能拦，按原流程继续。"""
        db_root = tmp_path / "wx_files"
        (db_root / "wxid_self_abcd" / "db_storage").mkdir(parents=True)
        svc, db = _make_service(tmp_path)
        created = self._patch_wx(monkeypatch, db_root)
        monkeypatch.setattr(wxdb_mod, "enumerate_weixin_pids", lambda: [11800])
        monkeypatch.setattr(wxdb_mod, "pid_account_match", lambda *a: None)
        monkeypatch.setattr(svc, "_adopt_legacy_keys", lambda *a, **k: None)
        try:
            with svc._listener_lock:
                ok = svc._open_listener_locked("wxid_self_abcd")
            assert ok is True
            assert len(created) == 1
        finally:
            svc.stop()
            db.close()

    def test_missing_account_dir_never_falls_back(self, monkeypatch,
                                                  tmp_path: Path):
        """指定账号目录不存在 → 明确降级，绝不悄悄用别的账号。"""
        db_root = tmp_path / "wx_files"
        db_root.mkdir()
        svc, db = _make_service(tmp_path)
        created = self._patch_wx(monkeypatch, db_root)
        try:
            with svc._listener_lock:
                ok = svc._open_listener_locked("wxid_gone_abcd")
            assert ok is False
            assert created == []
            assert "不存在或未登录" in svc._reason
        finally:
            db.close()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@pytest.fixture()
def api(tmp_path, monkeypatch):
    default_dir = tmp_path / "default_cache"
    monkeypatch.setattr(config_mod, "CACHE_DIR_DEFAULT", default_dir)
    monkeypatch.setattr(ws_mod, "CACHE_DIR_DEFAULT", default_dir)

    db = Database(str(tmp_path / "api.sqlite3"))
    svc = WeChatService(db, Aggregator(lambda unit: None))
    deps.configure(db=db, wechat=svc, pipeline=None, sender=None,
                   notifier=Notifier())
    app = FastAPI()
    for r in ALL_ROUTERS:
        app.include_router(r)
    client = TestClient(app)
    yield type("Api", (), {"client": client, "db": db, "svc": svc,
                           "default_dir": default_dir})()
    deps.configure(db=None, wechat=None, pipeline=None, sender=None,
                   notifier=None)
    db.close()


class TestCacheDirApi:
    def test_get_returns_default(self, api):
        r = api.client.get("/api/system/cache")
        assert r.status_code == 200
        body = r.json()
        assert body["is_default"] is True
        assert body["dir"] == str(api.default_dir)
        assert body["configured"] == ""
        assert body["active"] is False

    def test_put_custom_dir_persists(self, api, tmp_path):
        target = tmp_path / "wx_cache"
        r = api.client.put("/api/system/cache", json={"dir": str(target)})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dir"] == str(target.resolve())
        assert body["is_default"] is False
        assert target.is_dir()
        # 再读一次，确认落库
        again = api.client.get("/api/system/cache").json()
        assert again["dir"] == str(target.resolve())

    def test_put_empty_restores_default(self, api, tmp_path):
        api.client.put("/api/system/cache", json={"dir": str(tmp_path / "x")})
        r = api.client.put("/api/system/cache", json={"dir": ""})
        assert r.status_code == 200
        assert r.json()["is_default"] is True
        assert r.json()["dir"] == str(api.default_dir)

    def test_put_relative_dir_resolved(self, api):
        r = api.client.put("/api/system/cache", json={"dir": "test_cache_rel"})
        assert r.status_code == 200, r.text
        assert r.json()["dir"] == str((config_mod.BASE_DIR / "test_cache_rel").resolve())
        # 清理：恢复默认，避免在工作区留下空目录
        api.client.put("/api/system/cache", json={"dir": ""})
        try:
            (config_mod.BASE_DIR / "test_cache_rel").rmdir()
        except OSError:
            pass

    def test_put_rejects_file_path(self, api, tmp_path):
        file_path = tmp_path / "a.txt"
        file_path.write_text("x", encoding="utf-8")
        r = api.client.put("/api/system/cache", json={"dir": str(file_path)})
        assert r.status_code == 400
        assert "不是文件夹" in r.json()["detail"]
        # 失败不得写入配置
        assert api.db.get_setting(config_mod.KEY_RUNTIME_CACHE_DIR, "") == ""

    def test_put_rejects_too_long(self, api):
        r = api.client.put("/api/system/cache", json={"dir": "x" * 260})
        assert r.status_code == 400
        assert "路径过长" in r.json()["detail"]
