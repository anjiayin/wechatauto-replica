# -*- coding: utf-8 -*-
"""更新器纯逻辑守门：版本判定 / 验签 / 路径穿越防护。

updater.py 作为独立脚本用 importlib 加载（它位于 packaging/，不在 app 包内）。
"""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

UPDATER = Path(__file__).resolve().parents[2] / "packaging" / "updater.py"


@pytest.fixture(scope="module")
def updater():
    spec = importlib.util.spec_from_file_location("p1_updater", UPDATER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# ---- 安装根定位（与后端 update_service.bundle_root 同一优先级） ----
def test_resolve_bundle_explicit_wins(updater, monkeypatch, tmp_path):
    monkeypatch.delenv("KEFU_BUNDLE_ROOT", raising=False)
    assert updater.resolve_bundle(str(tmp_path)) == tmp_path
    assert updater.resolve_bundle("   ") == UPDATER.parent


def test_resolve_bundle_from_environment(updater, monkeypatch, tmp_path):
    monkeypatch.setenv("KEFU_BUNDLE_ROOT", str(tmp_path))
    assert updater.resolve_bundle() == tmp_path
    assert updater.resolve_bundle("") == tmp_path


# ---- 版本比较 ----
def test_version_numeric_order(updater):
    assert updater.compare_versions("1.9", "1.10") < 0
    assert updater.compare_versions("1.2.1", "1.2.2") < 0
    assert updater.compare_versions("1.3.0", "1.3.0") == 0
    assert updater.compare_versions("2.0.0", "1.9.9") > 0


def test_version_prerelease(updater):
    assert updater.compare_versions("1.3.0-beta", "1.3.0") < 0
    assert updater.compare_versions("1.3.0-rc.1", "1.3.0-beta") > 0


# ---- decide 判定规则 ----
def _manifest(latest="1.2.2", seq=17, channel="stable", min_sup="1.2.0"):
    return {"channel": channel, "sequence": seq, "latest": latest,
            "min_supported": min_sup, "notes": "test"}


def test_decide_no_update_when_current_is_latest(updater):
    assert updater.decide("1.2.2", _manifest(), "", 0) == "none"
    assert updater.decide("1.3.0", _manifest(), "", 0) == "none"  # 绝不降级


def test_decide_optional_and_forced(updater):
    assert updater.decide("1.2.1", _manifest(), "", 0) == "optional"
    assert updater.decide("1.1.9", _manifest(), "", 0) == "forced"  # < min_supported


def test_decide_channel_and_replay_guard(updater):
    assert updater.decide("1.2.1", _manifest(channel="beta"), "", 0) == "none"
    assert updater.decide("1.2.1", _manifest(seq=17), "", 17) == "none"
    assert updater.decide("1.2.1", _manifest(seq=17), "", 18) == "none"


def test_decide_ignored_version(updater):
    assert updater.decide("1.2.1", _manifest(), "1.2.2", 0) == "none"
    assert updater.decide("1.2.1", _manifest(), "1.2.1", 0) == "optional"


def test_pick_package(updater):
    m = {"packages": [
        {"type": "app", "version": "1.2.2", "from_versions": ["1.2.1"], "url": "app"},
        {"type": "full", "version": "1.2.2", "url": "full"},
    ]}
    assert updater.pick_package(m, "1.2.1")["type"] == "app"
    assert updater.pick_package(m, "1.0.0")["type"] == "full"


# ---- 验签 ----
def test_verify_manifest_ok_and_bad(updater):
    priv = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(
        priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    payload = b'{"latest":"1.2.2"}'
    sig = base64.b64encode(priv.sign(payload)).decode()
    keys = {"k1": pub_b64}
    assert updater.verify_manifest(payload, {"key_id": "k1", "signature": sig}, keys)[0]
    assert not updater.verify_manifest(payload + b" ", {"key_id": "k1", "signature": sig}, keys)[0]
    assert not updater.verify_manifest(payload, {"key_id": "unknown", "signature": sig}, keys)[0]


# ---- 路径穿越 ----
def test_safe_extract_blocks_traversal(updater, tmp_path):
    zpath = tmp_path / "evil.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../evil.txt", "pwned")
        zf.writestr("kefu-system/app/ok.pyc", b"ok")
    zpath.write_bytes(buf.getvalue())
    target = tmp_path / "out"
    ok, err = updater.safe_extract(zpath, target)
    assert not ok and "穿越" in err
    assert not (tmp_path.parent / "evil.txt").exists()


def test_safe_extract_normal(updater, tmp_path):
    zpath = tmp_path / "ok.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("kefu-system/app/x.pyc", b"ok")
    target = tmp_path / "out"
    ok, err = updater.safe_extract(zpath, target)
    assert ok and (target / "kefu-system" / "app" / "x.pyc").is_file()
