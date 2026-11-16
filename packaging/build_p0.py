# -*- coding: utf-8 -*-
"""P0 最小离线包构建器（只用新增文件装配，不改任何业务代码）。

用途
----
验证「嵌入式 CPython + 预置依赖/模型/ffmpeg/SILK 解码器 + 原样业务代码」
能否零改动跑通语音转写与图片解码。P0 刻意保持源码的两层目录布局，
因为 ``app/config.py`` 全部用 ``Path(__file__)`` 定位资源；正式三层布局
（``app/<版本>`` / ``runtime`` / ``data``）属于 P1，需先做路径解耦。

用法
----
    python packaging/build_p0.py \
        --embed-zip  D:/tmp/kefu-p0-cache/python-3.12.10-embed-amd64.zip \
        --site-packages D:/my_conda_envs/wechatauto/Lib/site-packages \
        --test-wav D:/tmp/kefu-p0-cache/test.wav \
        --out D:/tmp/kefu-p0-bundle

    # 重跑时跳过 471MB 依赖复制（只刷新业务代码/资源）
    python packaging/build_p0.py --skip-site

产物布局（全部在 --out，不进 git）
--------------------------------
    <out>/runtime/python/          嵌入式 CPython（改写 python312._pth）
    <out>/runtime/site-packages/  依赖（从本机已验证环境复制）
    <out>/runtime/ffmpeg.exe      内置 ffmpeg（PATH 由探针/启动器追加）
    <out>/kefu-system/app/        业务代码（原样）
    <out>/kefu-system/frontend/dist/
    <out>/kefu-system/tools/silk_v3_decoder.exe
    <out>/kefu-system/data/asr_models/model.int8.onnx + tokens.txt
    <out>/kefu-system/run.py
    <out>/kefu-system/version.txt
    <out>/wechatauto/             业务代码（原样，必须在 kefu-system 上一级）
    <out>/version.txt
    <out>/testdata/test_zh.wav
    <out>/selftest_p0.py
    <out>/build-info.json

注意：P0 用 3.12 embed 是为了与本机 cp312 wheel 的 ABI 一致、完全离线
可复现；方案定稿的 3.11 依赖装配需单独跑一次 pip download 验证（P1）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KEFU = REPO_ROOT / "kefu-system"
DEFAULT_OUT = Path("D:/tmp/kefu-p0-bundle")
DEFAULT_EMBED = Path("D:/tmp/kefu-p0-cache/python-3.12.10-embed-amd64.zip")
DEFAULT_SITE = Path("D:/my_conda_envs/wechatauto/Lib/site-packages")
DEFAULT_WAV = Path("D:/tmp/kefu-p0-cache/test.wav")

# 复制时跳过的缓存/工具包；顶层 site-packages 才排除工具包，避免误伤包内同名目录
SKIP_ANYWHERE = {"__pycache__", ".pytest_cache", ".git", ".mypy_cache", ".ruff_cache"}
SKIP_TOPLEVEL = {
    "pip", "setuptools", "pkg_resources", "_distutils_hack", "wheel",
    "pytest", "_pytest", "py", "iniconfig", "pluggy",
}


def _ignore_factory(site_root: Path):
    site_resolved = site_root.resolve()

    def _ignore(directory: str, names: list[str]) -> set[str]:
        out = {
            n for n in names
            if n in SKIP_ANYWHERE or n.endswith((".pyc", ".pyo"))
        }
        try:
            is_root = Path(directory).resolve() == site_resolved
        except OSError:
            is_root = False
        if is_root:
            out |= {
                n for n in names
                if n in SKIP_TOPLEVEL or n.startswith(("pip-", "setuptools-", "pytest-", "pluggy-", "iniconfig-"))
            }
            # .pth 不随包走：embed 版不启用 import site，且本机环境里
            # __editable__.wechatauto_replica-*.pth 会把 wechatauto 指回
            # 开发机仓库（客户机上不存在）。pywin32.pth 的职责改由 _pth 显式承担。
            out |= {n for n in names if n.endswith(".pth")}
        return out

    return _ignore


def _copy_tree(src: Path, dst: Path, label: str, site_root: Path | None = None) -> int:
    if not src.is_dir():
        raise SystemExit(f"[FATAL] 源目录不存在：{src}")
    started = time.monotonic()
    ignore = _ignore_factory(site_root) if site_root else _ignore_factory(src)
    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=ignore)
    size = sum(f.stat().st_size for f in dst.rglob("*") if f.is_file())
    print(f"[COPY] {label:<22} {size / 1024 / 1024:8.1f} MB  "
          f"{time.monotonic() - started:6.1f}s  -> {dst}")
    return size


def _copy_file(src: Path, dst: Path, label: str) -> int:
    if not src.is_file():
        raise SystemExit(f"[FATAL] 源文件不存在：{src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    size = dst.stat().st_size
    print(f"[COPY] {label:<22} {size / 1024 / 1024:8.1f} MB  -> {dst.name}")
    return size


def _find_ffmpeg() -> Path:
    """优先用本机 imageio-ffmpeg 自带的 exe（离线，不再联网下载）。"""
    try:
        sys.path.insert(0, str(DEFAULT_SITE))
        import imageio_ffmpeg
        candidate = Path(imageio_ffmpeg.get_ffmpeg_exe())
        if candidate.is_file():
            return candidate
    except Exception as exc:                     # noqa: BLE001
        print(f"[WARN] imageio_ffmpeg 定位失败：{exc}")
    found = shutil.which("ffmpeg")
    if found:
        return Path(found)
    raise SystemExit("[FATAL] 找不到 ffmpeg.exe，请用 --ffmpeg 指定")


def build(args: argparse.Namespace) -> None:
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    runtime = out / "runtime"
    python_dir = runtime / "python"
    site_dir = runtime / "site-packages"
    sizes: dict[str, int] = {}

    # 1) 嵌入式 CPython + _pth（四条路径，不启用 import site，DLL 由启动器显式挂）
    python_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.embed_zip) as zf:
        zf.extractall(python_dir)
    pth_files = list(python_dir.glob("python*._pth"))
    if len(pth_files) != 1:
        raise SystemExit(f"[FATAL] 期望 1 个 ._pth，实际 {pth_files}")
    pth = pth_files[0]
    # 关键：embed 版不执行 site-packages/*.pth，pywin32 依赖的三个子目录
    # 必须显式写进 _pth，否则 `import win32ui` 直接 ModuleNotFoundError。
    # 正式包若改用 pip --target，同样要显式装配（target 目录的 .pth 不生效）。
    pth.write_text(
        "python312.zip\n"
        ".\n"
        "..\\site-packages\n"
        "..\\site-packages\\win32\n"
        "..\\site-packages\\win32\\lib\n"
        "..\\site-packages\\pythonwin\n"
        "..\\..\\kefu-system\n"
        "..\\..\n",
        encoding="ascii",
    )
    print(f"[PTH ] {pth.name} -> site-packages + pywin32 三目录 + kefu-system + bundle 根")
    sizes["embed_python"] = sum(f.stat().st_size for f in python_dir.rglob("*") if f.is_file())

    # 2) 依赖（可跳过）
    if args.skip_site and site_dir.is_dir():
        print("[SKIP] site-packages 已存在，跳过复制")
        sizes["site_packages"] = sum(f.stat().st_size for f in site_dir.rglob("*") if f.is_file())
    else:
        if site_dir.is_dir():
            # 仅清理构建输出目录内的依赖副本，确保排除规则对旧残留也生效
            if site_dir.name != "site-packages" or site_dir.parent.name != "runtime":
                raise SystemExit(f"[FATAL] 拒绝清理非构建目录：{site_dir}")
            shutil.rmtree(site_dir)
        sizes["site_packages"] = _copy_tree(args.site_packages, site_dir,
                                            "site-packages", args.site_packages)

    # 3) 外部程序与离线资源
    ffmpeg_src = args.ffmpeg or _find_ffmpeg()
    sizes["ffmpeg"] = _copy_file(ffmpeg_src, runtime / "ffmpeg.exe", "ffmpeg.exe")

    asr_src = KEFU / "data" / "asr_models"
    asr_dst = out / "kefu-system" / "data" / "asr_models"
    asr_dst.mkdir(parents=True, exist_ok=True)
    for name in ("model.int8.onnx", "tokens.txt"):
        sizes[f"asr/{name}"] = _copy_file(asr_src / name, asr_dst / name, f"asr/{name}")

    sizes["silk_decoder"] = _copy_file(
        KEFU / "tools" / "silk_v3_decoder.exe",
        out / "kefu-system" / "tools" / "silk_v3_decoder.exe",
        "silk_v3_decoder.exe",
    )

    # 4) 业务代码：保持两层布局，一行不改
    sizes["app"] = _copy_tree(KEFU / "app", out / "kefu-system" / "app", "kefu-system/app")
    sizes["frontend"] = _copy_tree(KEFU / "frontend" / "dist",
                                   out / "kefu-system" / "frontend" / "dist",
                                   "frontend/dist")
    sizes["wechatauto"] = _copy_tree(REPO_ROOT / "wechatauto", out / "wechatauto", "wechatauto")
    _copy_file(KEFU / "run.py", out / "kefu-system" / "run.py", "run.py")
    _copy_file(REPO_ROOT / "version.txt", out / "version.txt", "version.txt")
    _copy_file(REPO_ROOT / "version.txt", out / "kefu-system" / "version.txt", "version.txt (app)")
    if args.test_wav and args.test_wav.is_file():
        _copy_file(args.test_wav, out / "testdata" / "test_zh.wav", "test_zh.wav")

    # 5) 自检探针 + 构建信息
    _copy_file(REPO_ROOT / "packaging" / "selftest_p0.py", out / "selftest_p0.py", "selftest_p0.py")
    info = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "out": str(out),
        "python": "3.12.10-embed-amd64 (P0 ABI 替身；定稿为 3.11，见 P1)",
        "site_packages_source": str(args.site_packages),
        "ffmpeg_source": str(ffmpeg_src),
        "layout": "P0 原两层布局（app/ 在 kefu-system/ 下，wechatauto/ 在 bundle 根）",
        "sizes_bytes": sizes,
        "total_mb": round(sum(sizes.values()) / 1024 / 1024, 1),
    }
    (out / "build-info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[OK] 离线包就绪：{out}  共 {info['total_mb']} MB")
    print(f"[NEXT] \"{python_dir / 'python.exe'}\" \"{out / 'selftest_p0.py'}\"")


def main() -> None:
    p = argparse.ArgumentParser(description="P0 最小离线包构建器")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--embed-zip", type=Path, default=DEFAULT_EMBED)
    p.add_argument("--site-packages", type=Path, default=DEFAULT_SITE)
    p.add_argument("--ffmpeg", type=Path, default=None)
    p.add_argument("--test-wav", type=Path, default=DEFAULT_WAV)
    p.add_argument("--skip-site", action="store_true", help="复用已有 site-packages")
    build(p.parse_args())


if __name__ == "__main__":
    main()
