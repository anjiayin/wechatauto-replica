# -*- coding: utf-8 -*-
"""批次 12 测试：启动端口显式保存（--save 持久化到 settings 表）。

覆盖：

- 地址解析四级优先级：命令行 > 环境变量 > 已保存配置（settings 表）> 默认；
  ``settings_reader=None`` 时行为与旧版完全一致（回归保护）。
- settings 表保存值损坏（``"0"`` / ``"abc"`` / ``"70000"``）→ 中文报错
  （注明来源「已保存配置」）+ 退出码 2。
- ``--save`` 写库（``save_address``）：库不存在 → 退出码 2；写库成功后
  fake reader 能读到 ``run.port`` / ``run.host``；重复保存为覆盖写。

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_batch12_port_save.py -v --basetemp=D:/tmp/kefu-p1-1
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import run as run_module


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def make_db(tmp_path: Path) -> Path:
    """造一个含最小 settings 表的库文件（模拟 Database 首次启动后的产物）。"""
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


def make_reader(db_path: Path):
    """构造从指定库只读读 settings 的 fake reader（模拟 main 的注入函数）。

    与真实 ``run._read_saved_setting`` 同口径：库不存在 / 表不存在时返回
    空串，绝不让配置缺失挡住启动。
    """
    def reader(key: str) -> str:
        if not db_path.exists():
            return ""
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key=?",
                    (key,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return ""
        return str(row[0]) if row else ""
    return reader


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """隔离环境变量，避免宿主 KEFU_PORT/KEFU_HOST 干扰优先级断言。"""
    monkeypatch.delenv("KEFU_PORT", raising=False)
    monkeypatch.delenv("KEFU_HOST", raising=False)


# ---------------------------------------------------------------------------
# 四级优先级：命令行 > 环境变量 > 已保存配置 > 默认
# ---------------------------------------------------------------------------
class TestSavedConfigPriority:
    def test_settings_used_when_no_cli_no_env(self, tmp_path):
        db = make_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            [("run.host", "0.0.0.0"), ("run.port", "9000")])
        conn.commit()
        conn.close()
        assert run_module.resolve_address(
            [], settings_reader=make_reader(db)) == ("0.0.0.0", 9000)

    def test_cli_beats_env_and_settings(self, tmp_path, monkeypatch):
        db = make_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO settings (key, value) VALUES "
                     "('run.port', '9000')")
        conn.commit()
        conn.close()
        monkeypatch.setenv("KEFU_PORT", "9001")
        assert run_module.resolve_address(
            ["--port", "9100"], settings_reader=make_reader(db))[1] == 9100

    def test_env_beats_settings(self, tmp_path, monkeypatch):
        db = make_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO settings (key, value) VALUES "
                     "('run.port', '9000')")
        conn.commit()
        conn.close()
        monkeypatch.setenv("KEFU_PORT", "9001")
        assert run_module.resolve_address(
            [], settings_reader=make_reader(db))[1] == 9001

    def test_settings_only_host_used_port_falls_default(self, tmp_path):
        """只保存过 host：端口没有已保存值 → 落默认 8765，host 用已保存值。"""
        db = make_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO settings (key, value) VALUES "
                     "('run.host', '0.0.0.0')")
        conn.commit()
        conn.close()
        assert run_module.resolve_address(
            [], settings_reader=make_reader(db)) == ("0.0.0.0", 8765)

    def test_settings_empty_value_falls_to_default(self, tmp_path):
        """reader 返回空串（键不存在 / 库不存在容错）→ 默认值。"""
        assert run_module.resolve_address(
            [], settings_reader=lambda key: "") == ("127.0.0.1", 8765)

    def test_reader_none_regression_same_as_legacy(self, monkeypatch):
        """settings_reader=None：行为与旧版完全一致（CLI > 环境变量 > 默认）。"""
        monkeypatch.setenv("KEFU_PORT", "9001")
        monkeypatch.setenv("KEFU_HOST", "0.0.0.0")
        assert run_module.resolve_address([]) == ("0.0.0.0", 9001)
        assert run_module.resolve_address(
            ["--host", "127.0.0.1", "--port", "9100"]) == ("127.0.0.1", 9100)
        monkeypatch.delenv("KEFU_PORT", raising=False)
        monkeypatch.delenv("KEFU_HOST", raising=False)
        assert run_module.resolve_address([]) == ("127.0.0.1", 8765)

    def test_saved_config_never_blocks_when_reader_returns_empty(self, tmp_path):
        """库文件不存在（reader 容错返回空串）→ 绝不让配置缺失挡住启动。"""
        missing = tmp_path / "no_such_db.sqlite3"
        assert run_module.resolve_address(
            [], settings_reader=make_reader(missing)) == ("127.0.0.1", 8765)


# ---------------------------------------------------------------------------
# 来源标注（启动日志用）
# ---------------------------------------------------------------------------
class TestOriginLabels:
    def test_each_level_has_its_origin_label(self, tmp_path, monkeypatch):
        db = make_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO settings (key, value) VALUES "
                     "('run.port', '9000')")
        conn.commit()
        conn.close()
        reader = make_reader(db)
        assert run_module._resolve_address_detail(
            ["--port", "9100"], reader).port_origin == "命令行参数"
        monkeypatch.setenv("KEFU_PORT", "9001")
        assert run_module._resolve_address_detail(
            [], reader).port_origin == "环境变量"
        monkeypatch.delenv("KEFU_PORT")
        assert run_module._resolve_address_detail(
            [], reader).port_origin == "已保存配置"
        assert run_module._resolve_address_detail(
            [], lambda key: "").port_origin == "默认值"


# ---------------------------------------------------------------------------
# settings 值损坏：中文报错（注明「已保存配置」）+ 退出码 2
# ---------------------------------------------------------------------------
class TestSavedConfigInvalid:
    @pytest.mark.parametrize("bad", ["0", "abc", "70000", "-1", "80.5"])
    def test_invalid_saved_port_exit_2(self, tmp_path, bad, capsys):
        db = make_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO settings (key, value) VALUES "
                     "('run.port', ?)", (bad,))
        conn.commit()
        conn.close()
        with pytest.raises(SystemExit) as exc:
            run_module.resolve_address([], settings_reader=make_reader(db))
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "已保存配置" in err

    def test_invalid_saved_port_reported_when_env_empty(
            self, tmp_path, capsys, monkeypatch):
        """环境变量为空串视为未设置 → 落到已保存配置并解析失败。"""
        db = make_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO settings (key, value) VALUES "
                     "('run.port', 'abc')")
        conn.commit()
        conn.close()
        monkeypatch.setenv("KEFU_PORT", "   ")
        with pytest.raises(SystemExit) as exc:
            run_module.resolve_address([], settings_reader=make_reader(db))
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# --save 写库（save_address 可注入路径）
# ---------------------------------------------------------------------------
class TestSaveAddress:
    def test_save_then_fake_reader_reads_back(self, tmp_path, capsys):
        db = make_db(tmp_path)
        run_module.save_address("0.0.0.0", 9000, db_path=db)
        out = capsys.readouterr().out
        assert "已保存监听地址 http://0.0.0.0:9000" in out
        assert "下次启动生效" in out
        # 保存后再次用 fake reader 读 run.port / run.host 能拿到值
        reader = make_reader(db)
        assert reader("run.port") == "9000"
        assert reader("run.host") == "0.0.0.0"
        # round-trip：下一次启动（无 CLI/环境变量）直接用已保存地址
        assert run_module.resolve_address(
            [], settings_reader=reader) == ("0.0.0.0", 9000)

    def test_save_overwrites_previous_value(self, tmp_path):
        """重复保存为覆盖写（upsert），后一次生效；恢复默认即写回 8765。"""
        db = make_db(tmp_path)
        run_module.save_address("0.0.0.0", 9000, db_path=db)
        run_module.save_address("127.0.0.1", 8765, db_path=db)
        reader = make_reader(db)
        assert reader("run.port") == "8765"
        assert run_module.resolve_address(
            [], settings_reader=reader) == ("127.0.0.1", 8765)

    def test_save_missing_db_exit_2(self, tmp_path, capsys):
        """库文件不存在（系统尚未启动过）→ 拒绝保存，退出码 2，不建库。"""
        missing = tmp_path / "not_created.sqlite3"
        with pytest.raises(SystemExit) as exc:
            run_module.save_address("127.0.0.1", 9000, db_path=missing)
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "配置库不存在" in err
        assert not missing.exists()      # 没有替 Database 建库

    def test_save_unwritable_path_exit_2(self, tmp_path, capsys):
        """路径存在但不是可写库（此处为目录）→ 写入失败，中文报错退出码 2。"""
        bogus = tmp_path / "kefu.sqlite3"
        bogus.mkdir()
        with pytest.raises(SystemExit) as exc:
            run_module.save_address("127.0.0.1", 9000, db_path=bogus)
        assert exc.value.code == 2
        assert "保存监听地址失败" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# --save 标记与 main 注入面（不真正启动 uvicorn）
# ---------------------------------------------------------------------------
class TestSaveFlagParsing:
    def test_save_flag_default_off(self):
        assert run_module._resolve_address_detail([]).save is False

    def test_save_flag_on(self):
        assert run_module._resolve_address_detail(["--save"]).save is True

    def test_help_mentions_save_and_saved_config(self, capsys):
        with pytest.raises(SystemExit) as exc:
            run_module._build_parser().parse_args(["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "--save" in out
        assert "已保存配置" in out
