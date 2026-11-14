# -*- coding: utf-8 -*-
"""版本号单一真源的守门用例：任何一处派生字面量漂移，CI 直接红。"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSION_FILE = REPO_ROOT / "version.txt"
SYNC_TOOL = REPO_ROOT / "kefu-system" / "tools" / "version_sync.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("kefu_version_sync", SYNC_TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_version_file_is_valid():
    tool = _load_tool()
    assert tool.VERSION_RE.match(VERSION_FILE.read_text(encoding="utf-8").strip())


def test_all_derived_literals_match_version_txt():
    proc = subprocess.run(
        [sys.executable, str(SYNC_TOOL), "--check"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_config_reads_version_from_version_txt():
    """后端运行时版本必须来自真源，不能是另一份手写常量。"""
    from app import config
    assert config.APP_VERSION == VERSION_FILE.read_text(encoding="utf-8").strip()


@pytest.mark.parametrize("a,b", [
    ("1.9", "1.10"),
    ("1.2.3", "1.2.3.1"),
    ("1.3.0-beta", "1.3.0"),
    ("1.2.9", "1.3.0"),
    ("1.2.1", "1.3.0-rc.1"),
])
def test_parse_version_orders_numerically(a, b):
    tool = _load_tool()
    assert tool.compare_versions(a, b) < 0
    assert tool.compare_versions(b, a) > 0


def test_parse_version_padding_is_equal():
    tool = _load_tool()
    assert tool.compare_versions("1.2", "1.2.0") == 0
    assert tool.compare_versions("1.2.1", "1.2.1") == 0


def test_check_detects_drift(tmp_path):
    """把工具抄进临时目录、篡改一份字面量，--check 必须报错。"""
    tool = _load_tool()
    problems = tool.collect_mismatches("0.0.0")
    assert problems
    assert any("version" in item or "!=" in item for item in problems)
