# -*- coding: utf-8 -*-
"""P1 交付包构建器：三层目录 + 全量字节码（无人工可读 .py）+ junction A/B 入口。

在 P0（build_p0.py）验证过的装配基础上做三件事：
  1. 三层布局：app/<版本>（可换）/ runtime（稳定重资源）/ data（用户数据，永不覆盖）
  2. 全量 pyc：业务代码 + site-packages 全部 compileall -b 生成 legacy .pyc，
     删除所有 .py（保留 .pyd/.dll/.json/模板等非源码资源）
  3. 固定入口 junction：app/current → app/<版本>，embed 的 ._pth 写死 current，
     A/B 切换只需重建 junction（后端停止时操作）

为什么不用 PYTHONPATH 注入版本路径：embed python 处于 ._pth 隔离模式，
实测完全忽略 PYTHONPATH；故此路线走 junction，已实测 .Delete() 安全切换。

用法
----
    python packaging/build_p1.py                       # 全量（含依赖复制 + pyc 化）
    python packaging/build_p1.py --skip-site          # 复用已有 runtime/site-packages（已 pyc 化则加 --reuse-pyc）
    python packaging/build_p1.py --site-packages D:/... --embed-zip D:/...

产物（全部在 --out，不进 git）
--------------------------------
    <out>/launcher.pyc / updater.pyc / selftest_p1.pyc
    <out>/current.json
    <out>/app/current -> app/<version>       （junction）
    <out>/app/<version>/kefu-system/{app/*.pyc, run.pyc, frontend/dist, tools, version.txt}
    <out>/app/<version>/wechatauto/*.pyc
    <out>/runtime/python/  site-packages/  asr_models/  *.exe
    <out>/data/                                  用户数据（空）
    <out>/start_kefu.bat                         CMD 一键启动（GBK+CRLF）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KEFU = REPO_ROOT / "kefu-system"
DEFAULT_OUT = Path("D:/tmp/kefu-p1-bundle")
DEFAULT_EMBED = Path("D:/tmp/kefu-p0-cache/python-3.12.10-embed-amd64.zip")
DEFAULT_SITE = Path("D:/my_conda_envs/wechatauto/Lib/site-packages")
DEFAULT_WAV = Path("D:/tmp/kefu-p0-cache/test.wav")

SKIP_ANYWHERE = {"__pycache__", ".pytest_cache", ".git", ".mypy_cache", ".ruff_cache"}
SKIP_TOPLEVEL = {
    "pip", "setuptools", "pkg_resources", "_distutils_hack", "wheel",
    "pytest", "_pytest", "py", "iniconfig", "pluggy",
}


def _read_version() -> str:
    return (REPO_ROOT / "version.txt").read_text(encoding="utf-8").strip()


def _ignore_factory(site_root: Path):
    site_resolved = site_root.resolve()

    def _ignore(directory: str, names: list[str]) -> set[str]:
        out = {n for n in names if n in SKIP_ANYWHERE or n.endswith((".pyc", ".pyo"))}
        try:
            is_root = Path(directory).resolve() == site_resolved
        except OSError:
            is_root = False
        if is_root:
            out |= {n for n in names if n in SKIP_TOPLEVEL
                    or n.startswith(("pip-", "setuptools-", "pytest-", "pluggy-", "iniconfig-"))}
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
    print(f"[COPY] {label:<24} {size / 1024 / 1024:8.1f} MB  {time.monotonic()-started:6.1f}s")
    return size


def _copy_file(src: Path, dst: Path, label: str) -> int:
    if not src.is_file():
        raise SystemExit(f"[FATAL] 源文件不存在：{src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"[COPY] {label:<24} {dst.stat().st_size / 1024 / 1024:8.1f} MB  -> {dst.name}")
    return dst.stat().st_size


def _find_ffmpeg() -> Path:
    try:
        import sys as _sys
        _sys.path.insert(0, str(DEFAULT_SITE))
        import imageio_ffmpeg
        cand = Path(imageio_ffmpeg.get_ffmpeg_exe())
        if cand.is_file():
            return cand
    except Exception as exc:                     # noqa: BLE001
        print(f"[WARN] imageio_ffmpeg 定位失败：{exc}")
    found = shutil.which("ffmpeg")
    if found:
        return Path(found)
    raise SystemExit("[FATAL] 找不到 ffmpeg.exe，请用 --ffmpeg 指定")


def _compiler_python(python_dir: Path) -> Path:
    """返回用于编译 pyc 的解释器：**必须**是 bundle 内置的 embed python。

    关键（2026-11-18 事故）：pyc 的 magic number 必须与运行时解释器一致，否则
    启动即 ``Bad magic number in .pyc file`` 秒退。曾因 PATH 里的 python 是
    3.13（veighna_studio）而 embed 是 3.12.10，重构打包后产出的 pyc 全部无法
    加载。用 embed 自带的 python.exe 编译可从根本上保证 magic 一致——无论
    build_p1.py 自身被哪个 Python 运行。
    """
    py = python_dir / ("python.exe" if os.name == "nt" else "python")
    if not py.is_file():
        raise SystemExit(f"[FATAL] 找不到 embed 解释器，无法编译 pyc：{py}")
    return py


def _assert_magic(py_exe: Path) -> str:
    """读编译器的 MAGIC_NUMBER；与 embed 运行库一致性由「用它自己编译」保证。"""
    r = subprocess.run([str(py_exe), "-c",
                        "import importlib.util,sys;sys.stdout.write(importlib.util.MAGIC_NUMBER.hex())"],
                       capture_output=True, text=True)
    if r.returncode != 0 or len(r.stdout.strip()) != 8:
        raise SystemExit(f"[FATAL] 读取 embed Python magic 失败：{r.stderr.strip()}")
    return r.stdout.strip()


def _pyc_all(root: Path, label: str, py_exe: Path) -> tuple[int, int]:
    """把 root 下所有 .py 编成 legacy .pyc（用 embed python），再删掉 .py。"""
    # 用 embed 解释器编译：产出 pyc 的 magic 与运行时 100% 一致。
    r = subprocess.run(
        [str(py_exe), "-m", "compileall", "-b", "-q", "-f", "-j", "1", str(root)],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"[FATAL] {label} 编译失败：{(r.stderr or r.stdout).strip()[:800]}")
    py_files = list(root.rglob("*.py"))
    for f in py_files:
        f.unlink()
    pyc = len(list(root.rglob("*.pyc")))
    print(f"[PYC ] {label:<24} 编译并删除 {len(py_files)} 个 .py，现有 {pyc} 个 .pyc"
          f"（embed magic={_assert_magic(py_exe)}）")
    return len(py_files), pyc


def _make_junction(link: Path, target: Path) -> None:
    """建 app/current junction。用 PowerShell New-Item，删旧用 .Delete() 不碰目标。"""
    if link.exists() or link.is_symlink():
        # 用 .NET DirectoryInfo.Delete 安全移除 junction（不递归删目标内容）
        ps = (
            f"$i = Get-Item -LiteralPath '{link}' -Force -ErrorAction SilentlyContinue; "
            f"if ($i) {{ $i.Delete() }}"
        )
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, text=True, check=False)
    ps = (f"New-Item -ItemType Junction -Path '{link}' -Target '{target}' "
          f"-ErrorAction Stop | Out-Null")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"[FATAL] 创建 junction 失败：{r.stderr.strip()}")
    print(f"[LINK] {link.name} -> {target.name}")


def build(args: argparse.Namespace) -> None:
    version = _read_version()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    runtime = out / "runtime"
    python_dir = runtime / "python"
    site_dir = runtime / "site-packages"
    app_root = out / "app" / version
    kefu_dst = app_root / "kefu-system"
    wc_dst = app_root / "wechatauto"
    data_dir = out / "data"
    sizes: dict[str, int] = {}

    # 1) 嵌入式 CPython + ._pth
    # _pth 写死固定的 app/current 入口（junction），A/B 切换不动 ._pth。
    python_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.embed_zip) as zf:
        zf.extractall(python_dir)
    pth_files = list(python_dir.glob("python*._pth"))
    if len(pth_files) != 1:
        raise SystemExit(f"[FATAL] 期望 1 个 ._pth，实际 {pth_files}")
    # embed 解释器确定后，全程用它编译 pyc（magic 与运行时一致，防启动秒退）。
    compiler_py = _compiler_python(python_dir)
    print(f"[PYC ] 编译解释器 = embed {compiler_py.name} magic={_assert_magic(compiler_py)}")
    pth = pth_files[0]
    pth.write_text(
        "python312.zip\n"
        ".\n"
        "..\\site-packages\n"
        "..\\site-packages\\win32\n"
        "..\\site-packages\\win32\\lib\n"
        "..\\site-packages\\pythonwin\n"
        "..\\..\\app\\current\\kefu-system\n"
        "..\\..\\app\\current\n"
        "..\\..\n",                       # bundle 根（launcher/updater 所在）
        encoding="ascii",
    )
    print(f"[PTH ] {pth.name} -> site-packages + app/current（junction 固定入口）")

    # 2) 依赖（可跳过复用）
    if args.skip_site and site_dir.is_dir():
        print("[SKIP] site-packages 已存在，跳过复制")
    else:
        if site_dir.is_dir():
            if site_dir.name != "site-packages" or site_dir.parent.name != "runtime":
                raise SystemExit(f"[FATAL] 拒绝清理非构建目录：{site_dir}")
            shutil.rmtree(site_dir)
        sizes["site_packages"] = _copy_tree(args.site_packages, site_dir,
                                            "site-packages", args.site_packages)

    # 2b) pywin32 DLL 到 exe 旁（原生启动免注入）
    win32_sys = site_dir / "pywin32_system32"
    if win32_sys.is_dir():
        n = 0
        for dll in win32_sys.glob("*.dll"):
            shutil.copy2(dll, python_dir / dll.name)
            n += 1
        print(f"[DLL ] pywin32 -> python.exe 同目录：{n} 个")

    # 3) 只读重资源进 runtime 层（config.py 由 KEFU_ASSETS_DIR 指过来）
    ffmpeg_src = args.ffmpeg or _find_ffmpeg()
    sizes["ffmpeg"] = _copy_file(ffmpeg_src, runtime / "ffmpeg.exe", "ffmpeg.exe")
    asr_src = KEFU / "data" / "asr_models"
    asr_dst = runtime / "asr_models"
    asr_dst.mkdir(parents=True, exist_ok=True)
    for name in ("model.int8.onnx", "tokens.txt"):
        sizes[f"asr/{name}"] = _copy_file(asr_src / name, asr_dst / name, f"asr/{name}")
    sizes["silk"] = _copy_file(KEFU / "tools" / "silk_v3_decoder.exe",
                               runtime / "silk_v3_decoder.exe", "silk_v3_decoder.exe")

    # 4) 业务代码进版本层（两层相对结构保持：wechatauto 在 kefu-system 上一级）
    sizes["app"] = _copy_tree(KEFU / "app", kefu_dst / "app", "app/kefu-system/app")
    sizes["frontend"] = _copy_tree(KEFU / "frontend" / "dist",
                                   kefu_dst / "frontend" / "dist", "frontend/dist")
    _copy_file(KEFU / "run.py", kefu_dst / "run.py", "run.py")
    _copy_file(REPO_ROOT / "version.txt", kefu_dst / "version.txt", "version.txt (app)")
    sizes["wechatauto"] = _copy_tree(REPO_ROOT / "wechatauto", wc_dst, "app/wechatauto")

    # 5) 测试音频（仅构建验证用，发布时可删）
    if args.test_wav and args.test_wav.is_file():
        _copy_file(args.test_wav, out / "testdata" / "test_zh.wav", "test_zh.wav")
    _copy_file(REPO_ROOT / "version.txt", out / "version.txt", "version.txt (bundle)")

    # 6) pyc 化，删除所有人工可读 .py
    # site-packages：全量构建才编译；--skip-site 复用时它已是 pyc（无 .py 可编）。
    if not args.skip_site:
        _pyc_all(site_dir, "site-packages", compiler_py)
    else:
        print("[SKIP] site-packages 已是 pyc，跳过其编译")
    # 业务代码 + 根脚本：每次都重新复制并编译删源（源码随 main 变化）。
    _pyc_all(app_root, "app/<version>", compiler_py)
    for name in ("launcher.py", "updater.py", "selftest_p1.py"):
        src = REPO_ROOT / "packaging" / name
        _copy_file(src, out / name, name)
    _pyc_all(out, "bundle 根脚本", compiler_py)   # launcher/updater/selftest 也编译删源

    # 7) current.json + app/current junction
    (out / "current.json").write_text(
        json.dumps({"version": version, "active": version}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    _make_junction(out / "app" / "current", app_root)

    # 8) data 层（空目录起步，绝不覆盖）
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / ".gitkeep").write_text("", encoding="ascii")

    # 8b) 交付预设 seed + 应用图标（随包发布到 bundle 根）
    #     delivery_seed.json：鉴权生产地址 + 预置商机类型，由 launcher 注入
    #     KEFU_DELIVERY_SEED 让后端首次初始化时应用；app.ico 供托盘/快捷方式/安装包用。
    assets = REPO_ROOT / "installer" / "assets"
    for fname in ("delivery_seed.json", "app.ico", "update_config.json"):
        src = assets / fname
        if src.is_file():
            _copy_file(src, out / fname, fname)
            print(f"[ASSET] {fname} -> bundle 根")
        else:
            print(f"[WARN ] 缺少交付资源 {src}（跳过）")

    # 8c) 发布时可命令行覆盖更新地址/渠道（build_release 或手工传参）。
    #     update_config.json 始终是 bundle 根的一份 JSON；launcher 读取后注入
    #     KEFU_UPDATE_MANIFEST_URL / KEFU_UPDATE_CHANNEL 给后端与托盘更新器。
    ucfg_path = out / "update_config.json"
    if getattr(args, "manifest_url", "") or getattr(args, "channel", ""):
        try:
            ucfg = json.loads(ucfg_path.read_text(encoding="utf-8")) \
                if ucfg_path.is_file() else {}
        except Exception:                        # noqa: BLE001
            ucfg = {}
        if args.manifest_url:
            ucfg["manifest_url"] = args.manifest_url.strip().rstrip("/")
        if args.channel:
            ucfg["channel"] = args.channel.strip()
        ucfg_path.write_text(json.dumps(ucfg, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        print(f"[UPDT ] update_config.json -> url={ucfg.get('manifest_url')!r} "
              f"channel={ucfg.get('channel')!r}")

    # 9) CMD 启动脚本（GBK + CRLF）
    starter_src = REPO_ROOT / "packaging" / "start_p1.bat"
    if starter_src.is_file():
        _copy_file(starter_src, out / "start_kefu.bat", "start_kefu.bat")
        bat = out / "start_kefu.bat"
        raw = bat.read_bytes()
        # 源文件由 Write 工具生成是 UTF-8；CMD 需 GBK + CRLF，统一转换。
        try:
            txt = raw.decode("gbk")
        except UnicodeDecodeError:
            txt = raw.decode("utf-8")
        bat.write_bytes(txt.replace("\r\n", "\n").replace("\r", "\n")
                        .replace("\n", "\r\n").encode("gbk"))
        print("[BAT ] start_kefu.bat -> GBK + CRLF")

    info = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": version,
        "python": "3.12.10-embed-amd64",
        "layout": "P1 三层（app/<version> junction / runtime / data）+ 全量 pyc",
        "source_files_remaining": len(list(out.rglob("*.py"))),
        "sizes_bytes": sizes,
        "total_mb": round(sum(sizes.values()) / 1024 / 1024, 1),
    }
    (out / "build-info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[OK] P1 包就绪：{out}  共 {info['total_mb']} MB")
    print(f"[OK] 剩余 .py 文件：{info['source_files_remaining']}（应为 0）")
    print(f"[NEXT] \"{python_dir / 'python.exe'}\" \"{out / 'selftest_p1.pyc'}\"")


def main() -> None:
    p = argparse.ArgumentParser(description="P1 三层 + 全量 pyc 交付包构建器")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--embed-zip", type=Path, default=DEFAULT_EMBED)
    p.add_argument("--site-packages", type=Path, default=DEFAULT_SITE)
    p.add_argument("--ffmpeg", type=Path, default=None)
    p.add_argument("--test-wav", type=Path, default=DEFAULT_WAV)
    p.add_argument("--skip-site", action="store_true", help="复用已有 site-packages（已 pyc 化）")
    p.add_argument("--manifest-url", default="", help="OSS manifest.json 所在目录完整地址（发布时填）")
    p.add_argument("--channel", default="", help="更新渠道，默认 stable")
    build(p.parse_args())


if __name__ == "__main__":
    main()
