# -*- coding: utf-8 -*-
"""自动更新发布工具（发布机运行；私钥/AK 只在发布机，绝不进客户端包）。

一站式（技术方案 7/11 节）：
  1. 读 version.txt（版本唯一真源）→ 作为本次发布的 latest
  2. 从已构建的 P1 bundle 打 app 增量 zip（版本层 kefu-system + wechatauto，
     并以 bundle_root/ 前缀携带安装根白名单文件 launcher.pyc / updater.pyc /
     start_kefu.bat）
  3. 算 size / sha256
  4. 生成 manifest.json（channel / sequence / latest / packages / assets …）
  5. 用发布机 Ed25519 私钥对 manifest **原始字节**签名 → manifest.json.sig
     （签名约定与客户端 app/update_core.verify_manifest 完全一致）
  6. 生成 update_config.json（manifest 地址 + 渠道），供首次安装包内置
  7. 若指定 --oss-base 且未 --dry-run：用 ossutil 先传包 → 回读校验 → 再传
     manifest + sig（避免 manifest 指向尚不存在的包）

用法
----
    # 本地生成发布产物（不上传，可人工检查/测试）
    python packaging/build_release.py --bundle D:/tmp/kefu-p1-bundle \
        --out D:/tmp/kefu-release --sequence 17 --notes "修复 XXX" \
        --base-url https://wechat-kefu.51zuhuobao.com/kefu/stable \
        --key-id prod-2026 --private-key D:/secure/prod-2026_private.pem

    # 正式上传（发布机，需配好 ossutil 与 AK）
    python packaging/build_release.py ... --oss-base oss://your-bucket/kefu/stable

fail-closed 底线：
  - 版本号非法 / sequence <= 0 / 私钥缺失或类型错误 → 直接退出，不产 manifest
  - 签名必须先于任何上传；--dry-run 绝不触发 ossutil
  - 私钥路径校验：拒绝落在 bundle/out 目录内（防止误把私钥打进交付件）
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BUNDLE = Path("D:/tmp/kefu-p1-bundle")
DEFAULT_OUT = Path("D:/tmp/kefu-release")
DEFAULT_KEY_DIR = REPO_ROOT / "kefu-system" / "tools" / "license_keys"
VERSION_RE = __import__("re").compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?$")

# 增量包相对 bundle 的版本层内容：apply() 解压后要求 kefu-system/app 存在
APP_TREES = ("kefu-system", "wechatauto")
# 安装根白名单：增量包 bundle_root/ 前缀只允许这三个文件（随包修复安装根的
# launcher.pyc / updater.pyc / start_kefu.bat）。客户端 apply 侧
# （app/update_service.py 与 packaging/updater.py）以同一份名单为第二道闸，
# 白名单之外的内容一律拒绝写入安装根。
ROOT_BUNDLE_FILES = ("launcher.pyc", "updater.pyc", "start_kefu.bat")
BUNDLE_ROOT_DIR = "bundle_root"
# manifest 里 assets 段：runtime 层资源（内容寻址，随 P1 bundle 带出）
ASSET_FILES = {
    "asr-model": ("runtime/asr_models/model.int8.onnx", "sensevoice-int8"),
    "ffmpeg": ("runtime/ffmpeg.exe", "imageio-ffmpeg"),
    "silk-decoder": ("runtime/silk_v3_decoder.exe", "kn007-windows"),
}


# ---------------------------------------------------------------------------
# 纯逻辑（可单测，无外部 IO 副作用）
# ---------------------------------------------------------------------------
def manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """生成待签名/落盘的 manifest 原始字节（签名与存储必须用同一字节串）。"""
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def sign_manifest(payload: bytes, private_key: Any, key_id: str) -> dict[str, str]:
    """对 manifest 原始字节签名，返回客户端可直接用的 .sig 文档。"""
    signature = private_key.sign(payload)
    return {"key_id": key_id,
            "signature": base64.b64encode(signature).decode("ascii")}


def load_private_key(path: Path) -> Any:
    """读取 Ed25519 私钥；类型不符/损坏直接退出（fail-closed）。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    try:
        key = load_pem_private_key(path.read_bytes(), password=None)
    except Exception as exc:                      # noqa: BLE001
        raise SystemExit(f"[FATAL] 读取私钥失败：{exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("[FATAL] 私钥不是 Ed25519 类型")
    return key


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def build_manifest(version: str, sequence: int, channel: str, base_url: str,
                   from_versions: list[str], notes: str, min_supported: str,
                   min_wechat_version: str, requires_antivirus_off: bool,
                   packages: list[dict[str, Any]], assets: dict[str, Any]) -> dict[str, Any]:
    """组装 manifest 字典（字段与技术方案 7.1 / update_core.consume 一致）。"""
    base = base_url.strip().rstrip("/")
    return {
        "channel": channel or "stable",
        "sequence": int(sequence),
        "released_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime()),
        "latest": version,
        "min_supported": min_supported or "0.0.0",
        "min_wechat_version": min_wechat_version or "",
        "requires_antivirus_off": bool(requires_antivirus_off),
        "notes": notes or "",
        "manifest_url": base,
        "packages": packages,
        "assets": assets,
    }


def make_app_zip(version_dir: Path, dst: Path,
                 bundle: Path | None = None) -> None:
    """把 app/<版本>/ 下的版本层内容打成增量 zip（zip 根即 kefu-system/wechatauto）。

    ``bundle`` 给出安装根路径时，额外把安装根白名单文件（ROOT_BUNDLE_FILES）
    以 ``bundle_root/`` 前缀打进包内，使自动升级能覆盖安装根的 launcher.pyc /
    updater.pyc / start_kefu.bat。严格白名单：名单之外的一律不打；bundle 缺
    某个白名单文件则跳过该项（不报错），绝不把 update_config.json / version.txt
    / data 等其他安装根内容打进包。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for tree in APP_TREES:
            src = version_dir / tree
            if not src.is_dir():
                raise SystemExit(f"[FATAL] 版本层缺少 {tree}：{src}")
            for f in sorted(src.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(version_dir).as_posix())
        if bundle is not None:
            for name in ROOT_BUNDLE_FILES:
                f = Path(bundle) / name
                if not f.is_file():
                    print(f"[PKG ] bundle 根缺少 {name}，跳过（不阻断发布）")
                    continue
                zf.write(f, f"{BUNDLE_ROOT_DIR}/{name}")


def package_entry(pkg_type: str, version: str, file: Path, url: str,
                  from_versions: list[str] | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": pkg_type,
        "version": version,
        "url": url,
        "size": file.stat().st_size,
        "sha256": sha256_file(file),
    }
    if from_versions:
        entry["from_versions"] = from_versions
    return entry


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------
def _refuse_private_key_in(keys: Path, *dirs: Path) -> None:
    """拒绝私钥位于 bundle/out 目录内（防止误打进交付件）。"""
    try:
        k = keys.resolve()
        for d in dirs:
            root = d.resolve()
            if k == root or root in k.parents:
                raise SystemExit(f"[FATAL] 私钥不能放在交付目录内：{keys}")
    except OSError:
        pass


OSSUTIL_MISSING_HINT = (
    "[OSS ] 未找到 ossutil（PATH 里既无 ossutil 也无 ossutil64）。"
    "发布工具靠 PATH 查找 ossutil，不读 ~/.ossutilconfig 定位程序。\n"
    "       请先安装 ossutil 2.0（Windows x64）：\n"
    "         https://gosspublic.alicdn.com/ossutil/v2/2.4.1/ossutil-2.4.1-windows-amd64.zip\n"
    "       解压后把含 ossutil.exe 的目录加入 PATH，重开终端，再执行 "
    "`ossutil --version` 与 `ossutil ls oss://<bucket>` 验证。" )


def _find_ossutil() -> str | None:
    """定位 ossutil 可执行文件（1.x 叫 ossutil64，2.x 叫 ossutil）。"""
    return shutil.which("ossutil") or shutil.which("ossutil64")


def require_ossutil() -> str:
    """需要上传时调用：找不到 ossutil 立即给出可操作提示并中止（fail-loud）。"""
    exe = _find_ossutil()
    if not exe:
        raise SystemExit("[FATAL] " + OSSUTIL_MISSING_HINT)
    return exe


def _oss_upload(oss_base: str, local_file: Path, remote_name: str,
                no_cache: bool = False) -> bool:
    """用 ossutil 上传单个文件；返回是否成功。

    ``no_cache`` 只给 manifest / sig 设置 ``Cache-Control: no-cache``，是防止
    客户端拿到旧 manifest 的**优化项**，不是发布正确性的前提。ossutil 2.x 用
    ``--cache-control``（1.x 的 ``--meta`` 已被移除），故依次尝试两种写法；
    都不可用就降级为普通上传并告警，绝不因 header 设置失败阻断发布。

    降级不吞真实故障：最后兜底的普通上传本身仍严格检查 returncode，网络/
    权限等真实上传失败一律返回 False；包文件（no_cache=False）只有一次普通
    上传，行为与修复前完全一致。
    """
    ossutil = _find_ossutil()
    if not ossutil:
        print("[FATAL] " + OSSUTIL_MISSING_HINT)
        return False
    remote = f"{oss_base.rstrip('/')}/{remote_name}"
    base_cmd = [ossutil, "cp", str(local_file), remote, "-f"]

    # 1.x/2.x 写法按新→旧尝试；普通上传（包文件）不参与 header 尝试。
    variants: tuple[tuple[str, tuple[str, ...]], ...] = (
        (("2.x", ("--cache-control", "no-cache")),
         ("1.x", ("--meta", "Cache-Control:no-cache")))
        if no_cache else (("", ()),)
    )
    for label, extra in variants:
        r = subprocess.run(base_cmd + list(extra), capture_output=True, text=True)
        # ossutil 2.x 成功信息走 stdout，失败细节可能在任一输出流，二者都带上便于排障
        detail = (r.stderr.strip() or r.stdout.strip())[:500]
        if r.returncode == 0:
            print(f"[OSS ] 已上传 {remote_name}")
            return True
        if not no_cache:
            print(f"[OSS ] 上传失败 {remote_name}：{detail}")
            return False
        print(f"[OSS ] 设置 Cache-Control:no-cache 失败（ossutil {label} 写法）："
              f"{detail[:200]}；继续尝试其它写法")

    # 两种 header 写法均不可用：普通上传兜底。此处失败就是真实上传失败，必须 False。
    r = subprocess.run(base_cmd, capture_output=True, text=True)
    detail = (r.stderr.strip() or r.stdout.strip())[:500]
    if r.returncode != 0:
        print(f"[OSS ] 上传失败 {remote_name}：{detail}")
        return False
    print(f"[WARN] {remote_name} 无法设置 Cache-Control:no-cache"
          "（当前 ossutil 版本不支持上述写法），已降级为普通上传："
          "OSS 直连域名默认不强缓存，发布继续。")
    print(f"[OSS ] 已上传 {remote_name}")
    return True


def _oss_readback(oss_base: str, remote_name: str, dst: Path) -> bool:
    r = _find_ossutil()
    if not r:
        print("[FATAL] " + OSSUTIL_MISSING_HINT)
        return False
    p = subprocess.run([r, "cp", f"{oss_base.rstrip('/')}/{remote_name}", str(dst), "-f"],
                       capture_output=True, text=True)
    return p.returncode == 0 and dst.is_file()


def release(args: argparse.Namespace) -> None:
    # 需要真实上传时最先校验 ossutil，避免白跑打包/签名才在上传环节失败
    if getattr(args, "oss_base", "") and not getattr(args, "dry_run", False):
        require_ossutil()

    version = args.version.strip()
    if not VERSION_RE.match(version):
        raise SystemExit(f"[FATAL] 非法版本号：{version!r}")
    if int(args.sequence) <= 0:
        raise SystemExit("[FATAL] --sequence 必须为正整数（单调递增发布序号）")

    bundle = args.bundle.resolve()
    version_dir = bundle / "app" / version
    if not (version_dir / "kefu-system" / "app").is_dir():
        raise SystemExit(f"[FATAL] bundle 缺少版本层：{version_dir}")

    out = args.out.resolve()
    pkg_dir = out / "packages"
    pkg_dir.mkdir(parents=True, exist_ok=True)

    # 1) 增量包（bundle 根白名单文件随 bundle_root/ 前缀一并打进包）
    app_zip = pkg_dir / f"app-{version}.zip"
    make_app_zip(version_dir, app_zip, bundle=bundle)
    print(f"[PKG ] app 增量包：{app_zip}  {app_zip.stat().st_size / 1024 / 1024:.1f} MB")

    base = (args.base_url or "").strip().rstrip("/")
    packages: list[dict[str, Any]] = []
    if base:
        packages.append(package_entry("app", version, app_zip,
                                      f"{base}/packages/app-{version}.zip",
                                      args.from_version or []))

    # 全量安装包（可选）：用 build_installer 产出的 exe
    if args.setup_exe:
        setup = Path(args.setup_exe).resolve()
        if not setup.is_file():
            raise SystemExit(f"[FATAL] 找不到全量安装包：{setup}")
        if base:
            full_dst = pkg_dir / f"full-{version}.exe"
            if full_dst.resolve() != setup.resolve():
                shutil.copy2(setup, full_dst)
            packages.append(package_entry("full", version, full_dst,
                                          f"{base}/packages/full-{version}.exe"))

    # 2) assets 段（runtime 层资源哈希，供后续独立下发）
    assets: dict[str, Any] = {}
    for key, (rel, aver) in ASSET_FILES.items():
        f = bundle / rel
        if f.is_file():
            assets[key] = {"version": aver, "sha256": sha256_file(f),
                           "size": f.stat().st_size}

    # 3) manifest
    m = build_manifest(
        version=version, sequence=int(args.sequence), channel=args.channel,
        base_url=base, from_versions=args.from_version or [], notes=args.notes,
        min_supported=args.min_supported, min_wechat_version=args.min_wechat_version,
        requires_antivirus_off=not args.no_antivirus_warning,
        packages=packages, assets=assets)
    payload = manifest_bytes(m)
    (out / "manifest.json").write_bytes(payload)

    # 4) 签名（必须在任何上传之前）
    key_path = Path(args.private_key).resolve()
    _refuse_private_key_in(key_path, bundle, out)
    if not key_path.is_file():
        raise SystemExit(f"[FATAL] 找不到签名私钥：{key_path}")
    priv = load_private_key(key_path)
    sig = sign_manifest(payload, priv, args.key_id)
    (out / "manifest.json.sig").write_text(
        json.dumps(sig, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[SIG ] 已签名：key_id={args.key_id}（manifest {len(payload)} 字节）")

    # 5) update_config.json（首次安装包内置的地址）
    ucfg = {"manifest_url": base, "channel": args.channel or "stable"}
    (out / "update_config.json").write_text(
        json.dumps(ucfg, ensure_ascii=False, indent=2), encoding="utf-8")

    # 6) 发布清单（不含私钥路径之外的敏感信息）
    info = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": version, "sequence": int(args.sequence),
        "channel": args.channel or "stable", "base_url": base,
        "packages": [{k: v for k, v in p.items() if k != "sha256"} | {"sha256": p["sha256"]}
                     for p in packages],
        "assets": list(assets.keys()), "dry_run": bool(args.dry_run),
    }
    (out / "release-info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

    # 7) 上传（先包、回读、再 manifest+sig）
    if args.oss_base:
        if args.dry_run:
            print("[DRY ] 跳过 OSS 上传（--dry-run）")
        elif not base:
            raise SystemExit("[FATAL] 上传需要 --base-url（manifest 里的公网下载地址）")
        else:
            for p in packages:
                name = Path(p["url"]).name
                if not _oss_upload(args.oss_base, pkg_dir / name, f"packages/{name}"):
                    raise SystemExit(f"[FATAL] 包上传失败，已中止（manifest 未上传）：{name}")
                # 回读校验 sha256
                verify = out / (name + ".verify")
                if _oss_readback(args.oss_base, f"packages/{name}", verify) \
                        and sha256_file(verify) == p["sha256"]:
                    print(f"[OSS ] 回读校验通过：{name}")
                else:
                    raise SystemExit(f"[FATAL] 包回读校验失败，已中止：{name}")
            if not _oss_upload(args.oss_base, out / "manifest.json", "manifest.json", True) \
                    or not _oss_upload(args.oss_base, out / "manifest.json.sig",
                                       "manifest.json.sig", True):
                raise SystemExit("[FATAL] manifest 上传失败")
            print("[OSS ] 发布完成：manifest + sig 已上传")

    print(f"\n[OK] 发布产物就绪：{out}")
    print("[NEXT] 把 update_config.json 的 manifest_url 内置进首次安装包，"
          "或直接复制本文件覆盖 bundle 根同名文件后重新 build_installer。")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="自动更新发布工具（manifest + 签名 + 上传）")
    p.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE, help="P1 bundle 根目录")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="发布产物输出目录")
    p.add_argument("--version", default="", help="版本号，默认取 version.txt")
    p.add_argument("--sequence", type=int, required=True, help="单调递增发布序号")
    p.add_argument("--channel", default="stable", help="渠道 stable/beta")
    p.add_argument("--base-url", default="", help="OSS 上 manifest.json 所在目录公网地址")
    p.add_argument("--from-version", action="append", default=[],
                   help="增量包适用的源版本，可多次传；不传则适用所有版本")
    p.add_argument("--notes", default="", help="更新日志（前端展示）")
    p.add_argument("--min-supported", default="", help="最低可升级版本，低于则强制升级")
    p.add_argument("--min-wechat-version", default="", help="微信客户端兼容门槛")
    p.add_argument("--no-antivirus-warning", action="store_true",
                   help="不提示关杀软（仅代码签名后使用）")
    p.add_argument("--setup-exe", default="", help="全量安装包 exe 路径（可选）")
    p.add_argument("--key-id", default="prod-2026", help="签名公钥 key_id（须在 license.PUBLIC_KEYS）")
    p.add_argument("--private-key", default="", help="Ed25519 私钥 PEM 路径（发布机保管）")
    p.add_argument("--oss-base", default="", help="ossutil 上传目标，如 oss://bucket/kefu/stable")
    p.add_argument("--dry-run", action="store_true", help="只生成产物，不上传")
    args = p.parse_args(argv)
    if not args.version:
        args.version = (REPO_ROOT / "version.txt").read_text(encoding="utf-8").strip()
    if not args.private_key:
        args.private_key = str(DEFAULT_KEY_DIR / f"{args.key_id}_private.pem")
    release(args)


if __name__ == "__main__":
    main()
