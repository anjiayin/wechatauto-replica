# -*- coding: utf-8 -*-
"""交付 seed 守门：鉴权生产地址 + 两个商机类型只在交付态注入，且幂等。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.db as db_mod
from app import config


def _seed_file(tmp_path: Path, **overrides) -> Path:
    payload = {
        "settings": {"license.server_url": "https://wechat-kefu.51zuhuobao.com"},
        "opportunity_types": [
            {"name": "汽车配件", "description": "配件OE号判定", "enabled": 1},
            {"name": "客户投诉", "description": "投诉判定", "enabled": 1},
        ],
    }
    payload.update(overrides)
    p = tmp_path / "delivery_seed.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def test_seed_applies_server_and_types(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "DELIVERY_SEED_FILE", _seed_file(tmp_path))
    db = db_mod.Database(str(tmp_path / "kefu.sqlite3"))
    try:
        assert db.get_setting(config.KEY_LICENSE_SERVER_URL) == (
            "https://wechat-kefu.51zuhuobao.com")
        names = [r["name"] for r in db.list_types()]
        assert "汽车配件" in names and "客户投诉" in names
        # 打上标记
        assert db.get_setting(db_mod.KEY_DELIVERY_SEED_APPLIED)
    finally:
        db.close()


def test_seed_idempotent_no_duplicate_types(tmp_path, monkeypatch):
    seed = _seed_file(tmp_path)
    monkeypatch.setattr(db_mod, "DELIVERY_SEED_FILE", seed)
    path = str(tmp_path / "kefu.sqlite3")
    db = db_mod.Database(path)
    db.close()
    # 用户手动改了鉴权地址 + 改了类型描述
    db = db_mod.Database(path)
    db.set_setting(config.KEY_LICENSE_SERVER_URL, "http://my-custom:9000")
    rows = db.list_types()
    target = next(r for r in rows if r["name"] == "汽车配件")
    db.update_type(target["id"], {"description": "用户改过的描述"})
    db.close()
    # 再次初始化（seed 未变）：不应覆盖用户修改，也不应重复插入类型
    db = db_mod.Database(path)
    try:
        assert db.get_setting(config.KEY_LICENSE_SERVER_URL) == "http://my-custom:9000"
        assert sum(1 for r in db.list_types() if r["name"] == "汽车配件") == 1
        assert next(r for r in db.list_types()
                    if r["name"] == "汽车配件")["description"] == "用户改过的描述"
    finally:
        db.close()


def test_no_seed_env_no_effect(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "DELIVERY_SEED_FILE", None)
    db = db_mod.Database(str(tmp_path / "kefu.sqlite3"))
    try:
        # 开发态仍是内置默认（mock 地址），且无任何商机类型预设
        assert db.get_setting(config.KEY_LICENSE_SERVER_URL) == (
            config.DEFAULT_LICENSE_SERVER_URL)
        assert db.list_types() == []
    finally:
        db.close()
