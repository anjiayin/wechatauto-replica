# -*- coding: utf-8 -*-
"""把 P1 三层 bundle 编译成 Inno Setup 单文件安装包。

做法：注入版本/源/输出三个宏调用 ISCC，编译期间临时移除 app\\current
junction（否则 Inno 会跟着链接再打一份版本层），编译完无论成功失败都恢复。
data 层只有 .gitkeep 会进包，sqlite / secret.key / 授权状态绝不入包。

用法
----
    python packaging/build_installer.py                       # 默认 bundle -> D:/tmp/kefu-setup
    python packaging/build_installer.py --iscc "D:/ISCC.exe" --bundle D:/tmp/kefu-p1-bundle
    python packaging/build_installer.py --dry-run             # 只校验布局与 junction 处理，不调 ISCC

未安装 Inno Setup 6 时给出明确下载指引；--dry-run 可在任意机器上验证前置条件。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ISS = REPO_ROOT / "installer" / "kefu.iss"
DEFAULT_BUNDLE = Path("D:/tmp/kefu-p1-bundle")
DEFAULT_OUT = Path("D:/tmp/kefu-setup")
ISCC_CANDIDATES = (
    Path("C:/Program Files (x86)/Inno Setup 6/ISCC.exe"),
    Path("C:/Program Files/Inno Setup 6/ISCC.exe"),
    Path("D:/Program Files (x86)/Inno Setup 6/ISCC.exe"),
    Path("D:/Program Files/Inno Setup 6/ISCC.exe"),
)


def _ps(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=60)


def _junction_remove(link: Path) -> bool:
    """只移除 junction 本身（不碰目标目录）；不存在也算成功。"""
    ps = (
        f"$i=Get-Item -LiteralPath '{link}' -Force -ErrorAction SilentlyContinue; "
        f"if($i -and ($i.Attributes -band [IO.FileAttributes]::ReparsePoint)) "
        f"{{ $i.Delete(); 'REMOVED' }} else {{ 'NONE' }}"
    )
    r = _ps(ps)
    return r.returncode == 0


def _junction_create(link: Path, target: Path) -> bool:
    ps = (f"New-Item -ItemType Junction -Path '{link}' -Target '{target}' "
          f"-ErrorAction Stop | Out-Null")
    return _ps(ps).returncode == 0


def _find_iscc(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit if explicit.is_file() else None
    for p in ISCC_CANDIDATES:
        if p.is_file():
            return p
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def validate(bundle: Path) -> tuple[str, Path]:
    """校验 bundle 布局，返回 (version, version_dir)。"""
    if not ISS.is_file():
        raise SystemExit(f"[FATAL] 缺少安装脚本：{ISS}")
    current = bundle / "current.json"
    if not current.is_file():
        raise SystemExit(f"[FATAL] bundle 缺少 current.json：{bundle}")
    try:
        version = str(json.loads(current.read_text(encoding="utf-8"))["version"]).strip()
    except Exception as exc:                      # noqa: BLE001
        raise SystemExit(f"[FATAL] current.json 无法解析：{exc}") from exc
    if not version or any(c in version for c in ('"', "'", "\\", "/", ":")):
        raise SystemExit(f"[FATAL] 非法版本号：{version!r}")
    required = [
        bundle / "launcher.pyc", bundle / "updater.pyc",
        bundle / "start_kefu.bat", bundle / "version.txt",
        bundle / "runtime" / "python" / "python.exe",
        bundle / "runtime" / "site-packages",
        bundle / "runtime" / "asr_models" / "model.int8.onnx",
        bundle / "app" / version / "kefu-system" / "run.pyc",
        bundle / "app" / version / "kefu-system" / "app" / "update_core.pyc",
        bundle / "app" / version / "kefu-system" / "frontend" / "dist" / "index.html",
    ]
    missing = [str(p.relative_to(bundle)) for p in required if not p.exists()]
    if missing:
        raise SystemExit("[FATAL] bundle 缺文件：" + ", ".join(missing))
    # data 哨兵：保证 [Files] 段引用的路径存在，但不往包里放任何用户数据
    sentinel = bundle / "data" / ".gitkeep"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    if not sentinel.is_file():
        sentinel.write_text("", encoding="ascii")
    return version, bundle / "app" / version


def build(args: argparse.Namespace) -> None:
    bundle = args.bundle.resolve()
    out = args.out.resolve()
    version, version_dir = validate(bundle)
    out.mkdir(parents=True, exist_ok=True)

    iscc = _find_iscc(args.iscc)
    if iscc is None and not args.dry_run:
        raise SystemExit(
            "[FATAL] 未找到 Inno Setup 6 编译器。请安装 https://jrsoftware.org/isdl.php "
            "的 Inno Setup 6，或用 --iscc 指定 ISCC.exe 路径；"
            "仅验证布局可用 --dry-run。")

    link = bundle / "app" / "current"
    had_junction = link.exists()
    try:
        if link.exists():
            print("[LINK] 临时移除 app/current junction（避免重复打进安装包）")
            if not _junction_remove(link):
                raise SystemExit("[FATAL] 移除 junction 失败")
        if args.dry_run:
            print(f"[DRY ] 布局校验通过：版本 {version}，源 {bundle}")
            print(f"[DRY ] 跳过 ISCC 编译；ISCC = {iscc or '未安装'}")
            return
        cmd = [str(iscc), f"/dMyAppVersion={version}",
               f'/dSourcePath={bundle}', f'/dOutputDir={out}', str(ISS)]
        print(f"[ISCC] {' '.join(cmd)}")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            print(r.stdout[-4000:])
            print(r.stderr[-2000:])
            raise SystemExit(f"[FATAL] ISCC 编译失败（退出码 {r.returncode}）")
    finally:
        # 构建完（无论成败）都要恢复 junction，bundle 仍可被 selftest 使用
        if had_junction and version_dir.is_dir():
            if _junction_create(link, version_dir):
                print(f"[LINK] 已恢复 app/current -> {version_dir.name}")
            else:
                print("[WARN] junction 恢复失败，请手动重建（详见技术方案 4.2）")

    exe = out / f"WeChatKefu-Setup-{version}.exe"
    if not exe.is_file():
        raise SystemExit(f"[FATAL] 未找到安装包产物：{exe}")
    info = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": version,
        "source_bundle": str(bundle),
        "installer": str(exe),
        "size_bytes": exe.stat().st_size,
        "sha256": _sha256(exe),
        "install_dir": "%LOCALAPPDATA%\\WeChatKefu",
        "unsigned": True,
    }
    (out / "installer-build-info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] 安装包：{exe}  {info['size_bytes'] / 1024 / 1024:.1f} MB")
    print(f"[OK] SHA256：{info['sha256']}")


def main() -> None:
    p = argparse.ArgumentParser(description="P1 bundle -> Inno Setup 安装包")
    p.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--iscc", type=Path, default=None)
    p.add_argument("--dry-run", action="store_true", help="只校验布局与 junction 处理")
    build(p.parse_args())


if __name__ == "__main__":
    main()
