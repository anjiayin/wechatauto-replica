# -*- coding: utf-8 -*-
"""授权模式切换（`run.py --license-dev` / `--license-on`）测试。

覆盖：

- 写库助手 ``_write_saved_setting``：库不存在 → False + 中文提示；
  写入后能读回，重复写为覆盖写。
- ``--license-dev`` 写入的是**口令指纹**（库里不出现明文），且
  :func:`app.license.is_dev_token` 认它。
- ``--license-on`` 写回 ``"1"``。
- 未知开关返回 ``None``（不干预启动流程）。

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_license_mode_switch.py -v --basetemp=D:/tmp/kefu-lic
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import run as run_module
from app import license as lic


def make_db(tmp_path: Path) -> Path:
    """造一个含最小 settings 表的库文件。"""
    path = tmp_path / "kefu.sqlite3"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE settings ("
            "  key        TEXT PRIMARY KEY,"
            "  value      TEXT NOT NULL,"
            "  updated_at TEXT NOT NULL DEFAULT (datetime('now')))")
        conn.commit()
    finally:
        conn.close()
    return path


def read_setting(db: Path, key: str) -> str:
    """只读一个 settings 值（不存在返回空串）。"""
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    finally:
        conn.close()
    return str(row[0]) if row else ""


# ---------------------------------------------------------------------------
# 写库助手
# ---------------------------------------------------------------------------
def test_write_setting_missing_db(tmp_path, capsys):
    """库不存在：返回 False 并提示先启动一次，绝不替它建库。"""
    missing = tmp_path / "nope.sqlite3"
    assert run_module._write_saved_setting("license.enabled", "1",
                                          missing) is False
    assert not missing.exists()
    assert "配置库不存在" in capsys.readouterr().err


def test_write_setting_insert_then_update(tmp_path):
    """首次插入、再次覆盖写。"""
    db = make_db(tmp_path)
    assert run_module._write_saved_setting("license.enabled", "1", db) is True
    assert read_setting(db, "license.enabled") == "1"
    assert run_module._write_saved_setting("license.enabled", "abc",
                                          db) is True
    assert read_setting(db, "license.enabled") == "abc"


# ---------------------------------------------------------------------------
# 模式切换
# ---------------------------------------------------------------------------
def test_license_dev_requires_token(tmp_path, capsys):
    """--license-dev 必须出示正确口令，否则拒绝（客户无法自己关授权）。"""
    db = make_db(tmp_path)
    for bad in ("", "wrong", "cheche"):
        assert run_module.license_mode_command("--license-dev", db,
                                              token=bad) == 2
    assert read_setting(db, "license.enabled") == ""
    assert "需要正确的测试口令" in capsys.readouterr().err


def test_license_dev_writes_token_fingerprint(tmp_path, monkeypatch, capsys):
    """--license-dev 写指纹（不是明文），且被判定为测试口令。"""
    db = make_db(tmp_path)
    monkeypatch.setattr(lic, "DEV_TOKEN_SHA256", lic.token_digest("tmp-token"))
    assert run_module.license_mode_command("--license-dev", db,
                                          token="tmp-token") == 0
    stored = read_setting(db, "license.enabled")
    assert stored == lic.DEV_TOKEN_SHA256
    assert "cheche" not in stored
    assert lic.is_dev_token(stored) is True
    assert "测试模式" in capsys.readouterr().out


def test_license_on_restores_enforcement(tmp_path, capsys):
    """--license-on 写回 1，测试口令随之失效。"""
    db = make_db(tmp_path)
    run_module._write_saved_setting("license.enabled",
                                    lic.DEV_TOKEN_SHA256, db)
    assert run_module.license_mode_command("--license-on", db) == 0
    stored = read_setting(db, "license.enabled")
    assert stored == "1"
    assert lic.is_dev_token(stored) is False
    assert "强制授权" in capsys.readouterr().out


def test_unknown_flag_returns_none(tmp_path):
    """没命中的开关不干预启动。"""
    db = make_db(tmp_path)
    assert run_module.license_mode_command("--license", db) is None


def test_dev_fails_when_db_missing(tmp_path, monkeypatch, capsys):
    """库不存在时切模式失败并返回 2（不静默成功）。"""
    monkeypatch.setattr(lic, "DEV_TOKEN_SHA256", lic.token_digest("tmp-token"))
    missing = tmp_path / "nope.sqlite3"
    assert run_module.license_mode_command("--license-dev", missing,
                                          token="tmp-token") == 2
    assert "配置库不存在" in capsys.readouterr().err
