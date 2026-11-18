# -*- coding: utf-8 -*-
"""发布工具守门：manifest 结构/签名互认/增量包结构/fail-closed/私钥隔离。

build_release.py 位于 packaging/，用 importlib 加载；签名必须能被客户端
app/update_core.verify_manifest 用 license.PUBLIC_KEYS 验过（端到端互认）。
"""
from __future__ import annotations

import base64
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE = REPO_ROOT / "packaging" / "build_release.py"


@pytest.fixture(scope="module")
def br():
    spec = importlib.util.spec_from_file_location("build_release", RELEASE)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def keypair(tmp_path):
    priv = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    pem = tmp_path / "prod-2026_private.pem"
    pem.write_bytes(priv.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return priv, pub_b64, pem


def _version_bundle(tmp_path: Path, version: str = "1.2.2") -> Path:
    """在 tmp_path/bundle 建版本层；私钥留在 tmp_path 根（bundle 之外）。"""
    tmp_path = tmp_path / "bundle"
    vd = tmp_path / "app" / version
    (vd / "kefu-system" / "app").mkdir(parents=True)
    (vd / "kefu-system" / "app" / "run.pyc").write_bytes(b"PYC")
    (vd / "kefu-system" / "frontend" / "dist").mkdir(parents=True)
    (vd / "kefu-system" / "frontend" / "dist" / "index.html").write_text("ok")
    (vd / "wechatauto" / "uia").mkdir(parents=True)
    (vd / "wechatauto" / "uia" / "driver.pyc").write_bytes(b"PYC")
    (tmp_path / "runtime" / "asr_models").mkdir(parents=True)
    (tmp_path / "runtime" / "asr_models" / "model.int8.onnx").write_bytes(b"M" * 100)
    (tmp_path / "runtime" / "ffmpeg.exe").write_bytes(b"F" * 50)
    (tmp_path / "runtime" / "silk_v3_decoder.exe").write_bytes(b"S" * 10)
    (tmp_path / "current.json").write_text(json.dumps({"version": version}))
    return tmp_path


def _args(tmp_path, keypair, version="1.2.2", base="https://cdn.example.com/kefu/stable"):
    from argparse import Namespace
    _, _, pem = keypair
    return Namespace(
        bundle=tmp_path / "bundle", out=tmp_path / "release", version=version,
        sequence=17, channel="stable", base_url=base,
        from_version=["1.2.1"], notes="修复测试", min_supported="1.2.0",
        min_wechat_version="4.1.15.56", no_antivirus_warning=False,
        setup_exe="", key_id="prod-2026", private_key=str(pem),
        oss_base="", dry_run=True,
    )


def test_manifest_bytes_is_deterministic(br):
    m = {"b": 2, "a": 1, "中文": "值"}
    b1 = br.manifest_bytes(m)
    assert b1 == br.manifest_bytes(m)
    assert b1.decode("utf-8")  # 非 ASCII 不转义
    assert b1.startswith(b'{"a":1,"b":2,')  # sort_keys + 紧凑分隔


def test_sign_and_client_verify_roundtrip(br, keypair):
    priv, pub_b64, _ = keypair
    payload = br.manifest_bytes({"latest": "1.2.2", "sequence": 17})
    sig = br.sign_manifest(payload, priv, "prod-2026")
    assert sig["key_id"] == "prod-2026"
    assert base64.b64decode(sig["signature"])

    # 用客户端 update_core 的真实验签函数验（不是测试自己的实现）
    import sys
    if str(REPO_ROOT / "kefu-system") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "kefu-system"))
    from app.update_core import verify_manifest
    ok, reason = verify_manifest(payload, sig, {"prod-2026": pub_b64})
    assert ok and not reason
    # 篡改后必须验不过
    ok2, _ = verify_manifest(payload + b" ", sig, {"prod-2026": pub_b64})
    assert not ok2


def test_make_app_zip_structure_for_apply(br, tmp_path):
    bundle = _version_bundle(tmp_path)
    vd = bundle / "app" / "1.2.2"
    zpath = tmp_path / "app-1.2.2.zip"
    br.make_app_zip(vd, zpath)
    with zipfile.ZipFile(zpath) as zf:
        names = set(zf.namelist())
    # apply() 解压后要求 kefu-system/app 存在
    assert "kefu-system/app/run.pyc" in names
    assert "kefu-system/frontend/dist/index.html" in names
    assert any(n.startswith("wechatauto/") for n in names)
    # 不应含 runtime / data / 私钥
    assert not any(n.startswith(("runtime/", "data/")) for n in names)


def test_release_produces_verifiable_manifest(br, keypair, tmp_path):
    bundle = _version_bundle(tmp_path)
    args = _args(tmp_path, keypair)
    br.release(args)
    out = args.out
    payload = (out / "manifest.json").read_bytes()
    m = json.loads(payload)
    assert m["latest"] == "1.2.2" and m["sequence"] == 17
    assert m["channel"] == "stable" and m["requires_antivirus_off"] is True
    pkg = next(p for p in m["packages"] if p["type"] == "app")
    assert pkg["from_versions"] == ["1.2.1"]
    assert pkg["size"] > 0 and len(pkg["sha256"]) == 64
    # 包文件存在且哈希一致
    zp = out / "packages" / "app-1.2.2.zip"
    assert zp.is_file() and br.sha256_file(zp) == pkg["sha256"]

    # 签名用客户端函数验
    import sys
    if str(REPO_ROOT / "kefu-system") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "kefu-system"))
    from app.update_core import verify_manifest
    sig = json.loads((out / "manifest.json.sig").read_text(encoding="utf-8"))
    _, pub_b64, _ = keypair
    assert verify_manifest(payload, sig, {"prod-2026": pub_b64})[0]

    # update_config 落地
    ucfg = json.loads((out / "update_config.json").read_text(encoding="utf-8"))
    assert ucfg["manifest_url"] == "https://cdn.example.com/kefu/stable"
    assert ucfg["channel"] == "stable"


def test_release_fail_closed_bad_version(br, keypair, tmp_path):
    bundle = _version_bundle(tmp_path)
    args = _args(tmp_path, keypair)
    args.version = "../evil"
    with pytest.raises(SystemExit):
        br.release(args)
    args = _args(tmp_path, keypair)
    args.sequence = 0
    with pytest.raises(SystemExit):
        br.release(args)


def test_release_refuses_private_key_inside_bundle(br, keypair, tmp_path):
    bundle = _version_bundle(tmp_path)
    _, _, pem = keypair
    # 把私钥放进 bundle 内，必须拒绝
    inside = bundle / "leak_private.pem"
    inside.write_bytes(pem.read_bytes())
    args = _args(tmp_path, keypair)
    args.private_key = str(inside)
    with pytest.raises(SystemExit) as ei:
        br.release(args)
    assert "交付目录" in str(ei.value)


def test_no_base_url_yields_no_packages(br, keypair, tmp_path):
    """未给公网地址时不生成 packages（避免产出指向空的 manifest）。"""
    bundle = _version_bundle(tmp_path)
    args = _args(tmp_path, keypair, base="")
    br.release(args)
    m = json.loads((args.out / "manifest.json").read_bytes())
    assert m["packages"] == []
