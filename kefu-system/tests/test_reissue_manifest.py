# -*- coding: utf-8 -*-
"""重发 manifest 工具守门：只抬序号/补 from_versions，包条目绝不改动。

现场 1.3.4 事故：包内容已含微信修复，只是 manifest 的 from_versions 漏了
1.3.3，导致在野版本被迫走从未成功过的全量包。重发时必须复用线上好包，
本测试确保工具不会误改 url/size/sha256，也不会接受重放序号或坏签名。
"""
from __future__ import annotations

import base64
import importlib.util
import json
from argparse import Namespace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO_ROOT = Path(__file__).resolve().parents[2]
REISSUE = REPO_ROOT / "packaging" / "reissue_manifest.py"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("reissue_manifest", REISSUE)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _keypair(tmp_path):
    priv = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    pem = tmp_path / "prod-2026_private.pem"
    pem.write_bytes(priv.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return priv, pub_b64, pem


def _signed_manifest(priv, *, latest="1.3.4", sequence=1340,
                     from_versions=None):
    base = "https://cdn.example.com/kefu"
    m = {
        "channel": "stable", "sequence": sequence, "latest": latest,
        "min_supported": "0.0.0", "min_wechat_version": "",
        "requires_antivirus_off": True, "notes": "old",
        "manifest_url": base, "released_at": "2026-11-25T00:00:00+08:00",
        "packages": [
            {"type": "app", "version": latest,
             "url": f"{base}/packages/app-{latest}.zip", "size": 12345,
             "sha256": "a" * 64, "from_versions": from_versions or ["1.2.9"]},
            {"type": "full", "version": latest,
             "url": f"{base}/packages/full-{latest}.exe", "size": 999,
             "sha256": "b" * 64},
        ],
        "assets": {},
    }
    payload = json.dumps(m, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    sig = {"key_id": "prod-2026",
           "signature": base64.b64encode(priv.sign(payload)).decode()}
    return payload, sig, m


def _args(tmp_path, pem, sequence=1341):
    return Namespace(
        base_url="https://cdn.example.com/kefu", out=tmp_path / "out",
        sequence=sequence, from_version=["1.3.3", "1.3.2", "1.3.1"],
        notes="补齐升级路径", key_id="prod-2026", private_key=str(pem),
        oss_base="", dry_run=True)


def _stub_online(monkeypatch, tool, payload, sig):
    files = {"manifest.json": payload,
             "manifest.json.sig": json.dumps(sig).encode("utf-8")}
    monkeypatch.setattr(tool, "fetch_online",
                        lambda base, name, timeout=30.0: files[name])
    monkeypatch.setattr(tool, "PUBLIC_KEYS", {
        "prod-2026": base64.b64encode(
            Ed25519PrivateKey.generate().public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw)).decode()})


def test_reissue_repairs_from_versions_and_bumps_sequence(tool, monkeypatch,
                                                          tmp_path):
    priv, pub_b64, pem = _keypair(tmp_path)
    payload, sig, old = _signed_manifest(priv)
    monkeypatch.setattr(tool, "fetch_online", lambda base, name, timeout=30.0: {
        "manifest.json": payload,
        "manifest.json.sig": json.dumps(sig).encode("utf-8")}[name])
    monkeypatch.setattr(tool, "PUBLIC_KEYS", {"prod-2026": pub_b64})

    result = tool.reissue(_args(tmp_path, pem))
    out = json.loads((tmp_path / "out" / "manifest.json").read_text("utf-8"))
    assert result["old_sequence"] == 1340
    assert result["new_sequence"] == 1341
    assert out["sequence"] == 1341
    assert out["latest"] == "1.3.4"
    app = next(p for p in out["packages"] if p["type"] == "app")
    assert set(app["from_versions"]) == {"1.2.9", "1.3.1", "1.3.2", "1.3.3"}
    assert out["notes"] == "补齐升级路径"


def test_reissue_keeps_package_bytes_identical(tool, monkeypatch, tmp_path):
    priv, pub_b64, pem = _keypair(tmp_path)
    payload, sig, old = _signed_manifest(priv)
    monkeypatch.setattr(tool, "fetch_online", lambda base, name, timeout=30.0: {
        "manifest.json": payload,
        "manifest.json.sig": json.dumps(sig).encode("utf-8")}[name])
    monkeypatch.setattr(tool, "PUBLIC_KEYS", {"prod-2026": pub_b64})

    tool.reissue(_args(tmp_path, pem))
    out = json.loads((tmp_path / "out" / "manifest.json").read_text("utf-8"))
    for old_pkg, new_pkg in zip(old["packages"], out["packages"]):
        for key in ("type", "version", "url", "size", "sha256"):
            assert old_pkg[key] == new_pkg[key]


def test_reissue_client_verifies_new_signature(tool, monkeypatch, tmp_path):
    """端到端：新 manifest 能被客户端用公钥验过。"""
    from app.update_core import verify_manifest

    priv, pub_b64, pem = _keypair(tmp_path)
    payload, sig, _ = _signed_manifest(priv)
    monkeypatch.setattr(tool, "fetch_online", lambda base, name, timeout=30.0: {
        "manifest.json": payload,
        "manifest.json.sig": json.dumps(sig).encode("utf-8")}[name])
    monkeypatch.setattr(tool, "PUBLIC_KEYS", {"prod-2026": pub_b64})

    tool.reissue(_args(tmp_path, pem))
    new_payload = (tmp_path / "out" / "manifest.json").read_bytes()
    new_sig = json.loads((tmp_path / "out" / "manifest.json.sig").read_text("utf-8"))
    assert verify_manifest(new_payload, new_sig, {"prod-2026": pub_b64}) == (True, "")


def test_reissue_client_picks_incremental_for_stuck_versions(tool, monkeypatch,
                                                             tmp_path):
    """重发后 1.3.1/1.3.2/1.3.3 都选中 app 增量包，不再落到 full。"""
    from app.update_core import is_full_package, pick_package

    priv, pub_b64, pem = _keypair(tmp_path)
    payload, sig, _ = _signed_manifest(priv)
    monkeypatch.setattr(tool, "fetch_online", lambda base, name, timeout=30.0: {
        "manifest.json": payload,
        "manifest.json.sig": json.dumps(sig).encode("utf-8")}[name])
    monkeypatch.setattr(tool, "PUBLIC_KEYS", {"prod-2026": pub_b64})

    tool.reissue(_args(tmp_path, pem))
    out = json.loads((tmp_path / "out" / "manifest.json").read_text("utf-8"))
    for current in ("1.3.1", "1.3.2", "1.3.3"):
        pkg = pick_package(out, current)
        assert pkg is not None and pkg["type"] == "app"
        assert is_full_package(pkg) is False


def test_reissue_rejects_replayed_sequence(tool, monkeypatch, tmp_path):
    priv, pub_b64, pem = _keypair(tmp_path)
    payload, sig, _ = _signed_manifest(priv)
    monkeypatch.setattr(tool, "fetch_online", lambda base, name, timeout=30.0: {
        "manifest.json": payload,
        "manifest.json.sig": json.dumps(sig).encode("utf-8")}[name])
    monkeypatch.setattr(tool, "PUBLIC_KEYS", {"prod-2026": pub_b64})

    with pytest.raises(SystemExit, match="必须 > 线上"):
        tool.reissue(_args(tmp_path, pem, sequence=1340))


def test_reissue_rejects_bad_online_signature(tool, monkeypatch, tmp_path):
    priv, pub_b64, pem = _keypair(tmp_path)
    payload, sig, _ = _signed_manifest(priv)
    monkeypatch.setattr(tool, "fetch_online", lambda base, name, timeout=30.0: {
        "manifest.json": payload,
        "manifest.json.sig": json.dumps(sig).encode("utf-8")}[name])
    monkeypatch.setattr(tool, "PUBLIC_KEYS", {
        "prod-2026": base64.b64encode(
            Ed25519PrivateKey.generate().public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw)).decode()})

    with pytest.raises(SystemExit, match="验签失败"):
        tool.reissue(_args(tmp_path, pem))
