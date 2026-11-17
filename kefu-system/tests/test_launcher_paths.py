# -*- coding: utf-8 -*-
"""安装器场景守门：安装包不含 junction，启动器按 current.json 补建。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[2] / "packaging" / "launcher.py"


@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location("p1_launcher", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _setup_bundle(launcher, monkeypatch, tmp_path: Path, version: str = "1.2.3"):
    monkeypatch.setattr(launcher, "BUNDLE", tmp_path)
    monkeypatch.setattr(launcher, "CURRENT_JSON", tmp_path / "current.json")
    (tmp_path / "current.json").write_text(
        json.dumps({"version": version}), encoding="utf-8")
    calls: list[tuple[Path, Path]] = []

    def fake_rebuild(link: Path, target: Path):
        calls.append((link, target))
        return True, ""

    monkeypatch.setattr(launcher, "_rebuild_junction", fake_rebuild)
    return calls


def test_ensure_current_rebuilds_from_current_json(launcher, monkeypatch, tmp_path):
    calls = _setup_bundle(launcher, monkeypatch, tmp_path)
    run_pyc = tmp_path / "app" / "1.2.3" / "kefu-system" / "run.pyc"
    run_pyc.parent.mkdir(parents=True)
    run_pyc.write_bytes(b"")
    assert launcher._ensure_current() is True
    assert len(calls) == 1
    assert calls[0][0] == tmp_path / "app" / "current"
    assert calls[0][1] == tmp_path / "app" / "1.2.3"


def test_ensure_current_rejects_missing_version_dir(launcher, monkeypatch, tmp_path):
    calls = _setup_bundle(launcher, monkeypatch, tmp_path)
    assert launcher._ensure_current() is False
    assert calls == []


def test_ensure_current_rejects_invalid_version(launcher, monkeypatch, tmp_path):
    calls = _setup_bundle(launcher, monkeypatch, tmp_path, version="../evil")
    assert launcher._ensure_current() is False
    assert calls == []
