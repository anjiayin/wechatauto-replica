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
    dist = vd / "kefu-system" / "frontend" / "dist"
    (dist / "index.html").write_text("ok")
    (dist / "version.json").write_text(json.dumps({"version": version}))
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


def test_release_fails_fast_when_ossutil_missing(br, keypair, tmp_path, monkeypatch):
    """真实上传但缺 ossutil：应在打包前立即中止，且提示可操作的安装地址。"""
    _version_bundle(tmp_path)
    monkeypatch.setattr(br.shutil, "which", lambda name: None)
    args = _args(tmp_path, keypair)
    args.oss_base = "oss://chehche-statics1/wechat-kefu"
    args.dry_run = False
    with pytest.raises(SystemExit) as ei:
        br.release(args)
    msg = str(ei.value)
    assert "ossutil" in msg
    assert "PATH" in msg
    assert "2.4.1-windows-amd64.zip" in msg  # 明确给出下载指引


def test_dry_run_needs_no_ossutil(br, keypair, tmp_path, monkeypatch):
    """--dry-run 不应因缺 ossutil 而失败。"""
    _version_bundle(tmp_path)
    monkeypatch.setattr(br.shutil, "which", lambda name: None)
    args = _args(tmp_path, keypair)
    args.oss_base = "oss://chehche-statics1/wechat-kefu"
    args.dry_run = True
    br.release(args)  # 不抛异常


# ---------------------------------------------------------------------------
# ossutil 1.x/2.x 兼容：全程打桩 subprocess.run，不触碰真实 OSS 网络
# ---------------------------------------------------------------------------
class _Completed:
    """subprocess.CompletedProcess 的最小替身。"""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _stub_ossutil(br, monkeypatch, fake_run) -> list[list[str]]:
    """把 ossutil 查找与 subprocess.run 全部换成本地桩，返回调用记录。"""
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        return fake_run(cmd)

    monkeypatch.setattr(br, "_find_ossutil", lambda: "ossutil")
    monkeypatch.setattr(br.subprocess, "run", run)
    return calls


def test_oss_upload_2x_uses_cache_control_flag(br, monkeypatch, tmp_path):
    """ossutil 2.x 新写法一次成功：不再使用已移除的 --meta。"""
    calls = _stub_ossutil(br, monkeypatch, lambda cmd: _Completed())
    f = tmp_path / "manifest.json"
    f.write_text("{}", encoding="utf-8")

    assert br._oss_upload("oss://bucket/kefu", f, "manifest.json", True) is True
    assert len(calls) == 1
    assert calls[0][-2:] == ["--cache-control", "no-cache"]
    assert "--meta" not in calls[0]


def test_oss_upload_1x_legacy_meta_flag(br, monkeypatch, tmp_path):
    """ossutil 1.x 不认 --cache-control 时，回退 --meta 仍可设置 no-cache。"""
    def fake_run(cmd):
        if "--cache-control" in cmd:
            return _Completed(1, stderr="Error: unknown flag: --cache-control")
        return _Completed()

    calls = _stub_ossutil(br, monkeypatch, fake_run)
    f = tmp_path / "manifest.json"
    f.write_text("{}", encoding="utf-8")

    assert br._oss_upload("oss://bucket/kefu", f, "manifest.json", True) is True
    assert len(calls) == 2
    assert "--meta" in calls[1]
    assert calls[1][-1] == "Cache-Control:no-cache"


def test_oss_upload_falls_back_to_plain_when_headers_unsupported(
        br, monkeypatch, tmp_path, capsys):
    """两种 header 写法都被拒时降级为普通上传并告警，发布不应被阻断。"""
    def fake_run(cmd):
        for flag in ("--cache-control", "--meta"):
            if flag in cmd:
                return _Completed(1, stderr=f"Error: unknown flag: {flag}")
        return _Completed(stdout="upload ok")

    calls = _stub_ossutil(br, monkeypatch, fake_run)
    f = tmp_path / "manifest.json"
    f.write_text("{}", encoding="utf-8")

    assert br._oss_upload("oss://bucket/kefu", f, "manifest.json", True) is True
    assert len(calls) == 3
    assert "--cache-control" not in calls[-1] and "--meta" not in calls[-1]
    out = capsys.readouterr().out
    assert "[WARN]" in out and "Cache-Control:no-cache" in out
    assert "降级为普通上传" in out


@pytest.mark.parametrize("no_cache", [False, True])
def test_oss_upload_real_failure_returns_false(
        br, monkeypatch, tmp_path, capsys, no_cache):
    """真实上传失败（鉴权/网络）无论是否 no_cache 都必须返回 False。"""
    def fake_run(cmd):
        return _Completed(1, stderr="Error: AccessDenied: You have no write permission")

    calls = _stub_ossutil(br, monkeypatch, fake_run)
    f = tmp_path / "manifest.json"
    f.write_text("{}", encoding="utf-8")

    assert br._oss_upload("oss://bucket/kefu", f, "manifest.json", no_cache) is False
    out = capsys.readouterr().out
    assert "上传失败 manifest.json" in out
    assert "已上传" not in out
    assert "AccessDenied" in out
    assert len(calls) == (3 if no_cache else 1)


def test_release_manifest_header_failure_does_not_block(
        br, keypair, tmp_path, monkeypatch, capsys):
    """端到端：2.x 拒收 header flag 时 manifest/sig 仍能普通上传，发布完成。"""
    _version_bundle(tmp_path)
    uploaded: dict[str, bytes] = {}

    def fake_run(cmd):
        source, dest = cmd[2], cmd[3]
        if "--cache-control" in cmd or "--meta" in cmd:
            return _Completed(1, stderr="Error: unknown flag (ossutil 2.4.1)")
        if dest.startswith("oss://"):
            uploaded[dest] = Path(source).read_bytes()
            return _Completed(stdout="upload ok")
        # 回读：把上传时记录的内容写回本地，模拟真实下载
        Path(dest).write_bytes(uploaded[source])
        return _Completed(stdout="download ok")

    _stub_ossutil(br, monkeypatch, fake_run)
    args = _args(tmp_path, keypair)
    args.oss_base = "oss://bucket/kefu/stable"
    args.dry_run = False

    br.release(args)  # 不得抛 SystemExit

    assert any(k.endswith("/packages/app-1.2.2.zip") for k in uploaded)
    assert any(k.endswith("/manifest.json") for k in uploaded)
    assert any(k.endswith("/manifest.json.sig") for k in uploaded)
    out = capsys.readouterr().out
    assert "回读校验通过" in out
    assert "[WARN]" in out and "降级为普通上传" in out


def test_release_package_upload_failure_is_fail_closed(
        br, keypair, tmp_path, monkeypatch):
    """包上传失败必须中止，manifest 绝不能被发布。"""
    _version_bundle(tmp_path)
    uploaded: dict[str, bytes] = {}

    def fake_run(cmd):
        source, dest = cmd[2], cmd[3]
        if dest.startswith("oss://"):
            if "packages/" in dest:
                return _Completed(1, stderr="Error: AccessDenied")
            uploaded[dest] = Path(source).read_bytes()
        return _Completed()

    _stub_ossutil(br, monkeypatch, fake_run)
    args = _args(tmp_path, keypair)
    args.oss_base = "oss://bucket/kefu/stable"
    args.dry_run = False

    with pytest.raises(SystemExit) as ei:
        br.release(args)
    assert "包上传失败" in str(ei.value)
    assert not any(k.endswith("/manifest.json") or k.endswith("/manifest.json.sig")
                   for k in uploaded)


def test_release_readback_mismatch_is_fail_closed(
        br, keypair, tmp_path, monkeypatch):
    """回读 sha256 不一致必须中止，manifest 绝不能被发布。"""
    _version_bundle(tmp_path)
    uploaded: dict[str, bytes] = {}

    def fake_run(cmd):
        source, dest = cmd[2], cmd[3]
        if dest.startswith("oss://"):
            uploaded[dest] = Path(source).read_bytes()
            return _Completed()
        Path(dest).write_bytes(b"CORRUPT")  # 模拟回读内容损坏
        return _Completed()

    _stub_ossutil(br, monkeypatch, fake_run)
    args = _args(tmp_path, keypair)
    args.oss_base = "oss://bucket/kefu/stable"
    args.dry_run = False

    with pytest.raises(SystemExit) as ei:
        br.release(args)
    assert "回读校验失败" in str(ei.value)
    assert not any(k.endswith("/manifest.json") or k.endswith("/manifest.json.sig")
                   for k in uploaded)


# ---------------------------------------------------------------------------
# bundle_root：安装根 launcher.pyc / updater.pyc / start_kefu.bat 随增量包下发
# ---------------------------------------------------------------------------
def _bundle_root_files(bundle: Path) -> None:
    (bundle / "launcher.pyc").write_bytes(b"LAUNCHER-NEW")
    (bundle / "updater.pyc").write_bytes(b"UPDATER-NEW")
    (bundle / "start_kefu.bat").write_bytes(b"START-BAT-NEW")


def test_make_app_zip_includes_bundle_root_files(br, tmp_path):
    """bundle 根白名单文件以 bundle_root/ 前缀进包，内容与源文件一致。"""
    bundle = _version_bundle(tmp_path)
    _bundle_root_files(bundle)
    vd = bundle / "app" / "1.2.2"
    zpath = tmp_path / "app-1.2.2.zip"
    br.make_app_zip(vd, zpath, bundle=bundle)
    with zipfile.ZipFile(zpath) as zf:
        names = set(zf.namelist())
        contents = {n: zf.read(n) for n in names}
    for name, blob in (("launcher.pyc", b"LAUNCHER-NEW"),
                       ("updater.pyc", b"UPDATER-NEW"),
                       ("start_kefu.bat", b"START-BAT-NEW")):
        assert f"bundle_root/{name}" in names
        assert contents[f"bundle_root/{name}"] == blob
    # 版本层照旧
    assert "kefu-system/app/run.pyc" in names


def test_make_app_zip_never_packs_nonwhitelist_bundle_files(br, tmp_path):
    """严格白名单：bundle 根的 update_config.json / version.txt / data 等
    绝不进包；bundle_root/ 前缀下只有三个白名单文件。"""
    bundle = _version_bundle(tmp_path)
    _bundle_root_files(bundle)
    (bundle / "update_config.json").write_text("{}", encoding="utf-8")
    (bundle / "version.txt").write_text("1.2.2", encoding="utf-8")
    (bundle / "delivery_seed.json").write_text("{}", encoding="utf-8")
    (bundle / "data").mkdir()
    (bundle / "data" / "kefu.sqlite3").write_bytes(b"DB")
    (bundle / "evil.exe").write_bytes(b"EVIL")
    vd = bundle / "app" / "1.2.2"
    zpath = tmp_path / "app-1.2.2.zip"
    br.make_app_zip(vd, zpath, bundle=bundle)
    with zipfile.ZipFile(zpath) as zf:
        names = set(zf.namelist())
    forbidden = {"update_config.json", "version.txt", "delivery_seed.json",
                 "evil.exe", "data/kefu.sqlite3"}
    assert not any(n in forbidden or n.startswith("data/") for n in names)
    assert {n for n in names if n.startswith("bundle_root/")} == {
        "bundle_root/launcher.pyc", "bundle_root/updater.pyc",
        "bundle_root/start_kefu.bat"}


def test_make_app_zip_skips_missing_bundle_files(br, tmp_path):
    """bundle 缺白名单文件 → 跳过该项，不报错、不打空条目。"""
    bundle = _version_bundle(tmp_path)
    (bundle / "launcher.pyc").write_bytes(b"L")
    vd = bundle / "app" / "1.2.2"
    zpath = tmp_path / "app-1.2.2.zip"
    br.make_app_zip(vd, zpath, bundle=bundle)          # 不抛异常
    with zipfile.ZipFile(zpath) as zf:
        names = set(zf.namelist())
    assert "bundle_root/launcher.pyc" in names
    assert not any(n.startswith("bundle_root/")
                   and n != "bundle_root/launcher.pyc" for n in names)


def test_make_app_zip_without_bundle_stays_backward_compatible(br, tmp_path):
    """不传 bundle → 不产生 bundle_root/ 前缀，与旧行为完全一致。"""
    bundle = _version_bundle(tmp_path)
    _bundle_root_files(bundle)
    vd = bundle / "app" / "1.2.2"
    zpath = tmp_path / "app-1.2.2.zip"
    br.make_app_zip(vd, zpath)
    with zipfile.ZipFile(zpath) as zf:
        names = set(zf.namelist())
    assert not any(n.startswith("bundle_root/") for n in names)


def test_release_end_to_end_carries_bundle_root(br, keypair, tmp_path):
    """release() 编排：bundle 根白名单文件随 app 增量包一起发布。"""
    bundle = _version_bundle(tmp_path)
    _bundle_root_files(bundle)
    args = _args(tmp_path, keypair)
    br.release(args)
    with zipfile.ZipFile(args.out / "packages" / "app-1.2.2.zip") as zf:
        names = set(zf.namelist())
    assert "bundle_root/launcher.pyc" in names
    assert "bundle_root/updater.pyc" in names
    assert "bundle_root/start_kefu.bat" in names


# ---------------------------------------------------------------------------
# 前端版本硬校验：杜绝「编了前端但没进包 / 漏编前端」导致多版本同屏
# ---------------------------------------------------------------------------
def test_assert_frontend_version_ok(br, tmp_path):
    bundle = _version_bundle(tmp_path)
    br.assert_frontend_version(bundle / "app" / "1.2.2", "1.2.2")  # 不抛异常


def test_assert_frontend_version_rejects_stale_build(br, tmp_path):
    bundle = _version_bundle(tmp_path)
    with pytest.raises(SystemExit, match="前端版本不一致"):
        br.assert_frontend_version(bundle / "app" / "1.2.2", "1.3.3")


def test_assert_frontend_version_rejects_missing_file(br, tmp_path):
    bundle = _version_bundle(tmp_path)
    (bundle / "app" / "1.2.2" / "kefu-system" / "frontend" / "dist"
     / "version.json").unlink()
    with pytest.raises(SystemExit, match="npm run build"):
        br.assert_frontend_version(bundle / "app" / "1.2.2", "1.2.2")


def test_release_aborts_on_stale_frontend(br, keypair, tmp_path):
    bundle = _version_bundle(tmp_path)
    (bundle / "app" / "1.2.2" / "kefu-system" / "frontend" / "dist"
     / "version.json").write_text(json.dumps({"version": "1.2.1"}))
    args = _args(tmp_path, keypair)
    with pytest.raises(SystemExit, match="前端版本不一致"):
        br.release(args)
    assert not (args.out / "manifest.json").exists()
