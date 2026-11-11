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
        (tmp_path / "sub").mkdir()      # 子目录不计入（缓存目录是平铺文件）
        size, count = config_mod.dir_usage(tmp_path)
        assert size == 123
        assert count == 2

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
