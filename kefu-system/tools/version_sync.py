# -*- coding: utf-8 -*-
"""客户端版本号单一真源工具（唯一真源：仓库根 ``version.txt``）。

其余位置都是「派生字面量」，禁止手改，统一由本工具校验 / 同步::

    python kefu-system/tools/version_sync.py --check         # 校验（默认；CI / 构建前跑）
    python kefu-system/tools/version_sync.py --sync          # 把派生位置同步成真源的值
    python kefu-system/tools/version_sync.py --bump 1.2.2   # 发版：改真源 + 同步全部

前端还有一道构建期硬校验：``vite.config.ts`` 每次 dev/build 都读 version.txt
并比对 package.json / package-lock.json，不一致直接抛错。

故意不纳管（这些不是「客户端版本号」，改了反而错）:
  * README*.md 的 changelog 历史条目（v1.1.5.1 / v1.2.1 是历史记录）
  * license-server/app/__init__.py、app/api/client_api.py（独立后台服务自己的版本）
  * license-server/tests/*、tools/mock_license_server.py（测试夹具 / 模拟服务示例值）
  * kefu-system/docs/授权服务接口文档.md 里 payload 示例的 app_version（示例值）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSION_FILE = REPO_ROOT / "version.txt"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?$")

# (相对仓库根的路径, 带命名组 ver 的正则, 替换模板, 期望匹配次数)
TARGETS: list[tuple[str, str, str, int]] = [
    ("pyproject.toml",
     r'(?m)^version = "(?P<ver>[^"]+)"',
     'version = "{ver}"', 1),
    ("wechatauto/__init__.py",
     r'(?m)^__version__ = "(?P<ver>[^"]+)"',
     '__version__ = "{ver}"', 1),
    ("kefu-system/app/config.py",
     r'(?m)^_FALLBACK_VERSION = "(?P<ver>[^"]+)"',
     '_FALLBACK_VERSION = "{ver}"', 1),
    ("kefu-system/frontend/package.json",
     r'(?m)^  "version": "(?P<ver>[^"]+)",',
     '  "version": "{ver}",', 1),
    ("GUIDE.md",
     r'(?m)^(print\(wechatauto\.__version__\)\s+#\s+)(?P<ver>[0-9][0-9A-Za-z.\-]*)[^\r\n]*',
     r'\g<1>{ver}', 1),
]

LOCK_REL = "kefu-system/frontend/package-lock.json"
LOCK_HEAD_LIMIT = 400   # lock 头部恰好含根 version 与 packages[""].version
LOCK_VER_RE = re.compile(r'"version": "(?P<ver>[^"]+)"')


def _read(path: Path) -> str:
    """按字节读，保留 CRLF / LF 原样，避免同步时把整个文件的行尾改写。"""
    return path.read_bytes().decode("utf-8")


def _write(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def read_version() -> str:
    lines = _read(VERSION_FILE).splitlines()
    version = lines[0].strip() if lines else ""
    if not VERSION_RE.match(version):
        raise ValueError(f"version.txt 格式非法：{version!r}（应为 X.Y.Z 或 X.Y.Z-beta）")
    return version


def parse_version(text: str) -> tuple[tuple[int, ...], int, str]:
    """版本号 → 可比较元组：数值段比较、补齐到 4 段、稳定版大于同数字预发布版。"""
    m = re.fullmatch(r"(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.\-]+))?", text.strip())
    if not m:
        raise ValueError(f"非法版本号：{text!r}")
    nums = tuple(int(p) for p in m.group(1).split("."))
    nums = nums + (0,) * max(0, 4 - len(nums))
    pre = m.group(2)
    return nums, (1 if pre is None else 0), (pre or "")


def compare_versions(a: str, b: str) -> int:
    """返回 -1 / 0 / 1。**禁止字符串比较**：'1.10' < '1.9' 是错的。"""
    pa, pb = parse_version(a), parse_version(b)
    return (pa > pb) - (pa < pb)


def _lock_head_values(text: str) -> list[str]:
    return [m.group("ver") for m in LOCK_VER_RE.finditer(text[:LOCK_HEAD_LIMIT])][:2]


def collect_mismatches(version: str) -> list[str]:
    problems: list[str] = []
    for rel, pattern, _tpl, expected in TARGETS:
        path = REPO_ROOT / rel
        found = [m.group("ver") for m in re.finditer(pattern, _read(path))]
        if len(found) != expected:
            problems.append(f"{rel}：期望 {expected} 处版本字面量，实际 {len(found)} 处")
            continue
        for value in found:
            if value != version:
                problems.append(f"{rel}：{value} != {version}")

    lock_text = _read(REPO_ROOT / LOCK_REL)
    head_values = _lock_head_values(lock_text)
    if len(head_values) != 2:
        problems.append(f"{LOCK_REL}：头部未找到 2 处版本字面量（结构可能变了）")
    else:
        for value in head_values:
            if value != version:
                problems.append(f"{LOCK_REL}：{value} != {version}")
    data = json.loads(lock_text)
    for label, value in (
        ("root", data.get("version")),
        ('packages[""]', (data.get("packages") or {}).get("", {}).get("version")),
    ):
        if value != version:
            problems.append(f"{LOCK_REL} {label}：{value} != {version}")
    return problems


def sync(version: str) -> list[str]:
    changed: list[str] = []
    for rel, pattern, template, expected in TARGETS:
        path = REPO_ROOT / rel
        old = _read(path)
        new, count = re.subn(pattern, template.format(ver=version), old)
        if count != expected:
            raise RuntimeError(f"{rel}：期望替换 {expected} 处，实际 {count} 处（正则需更新）")
        if new != old:
            _write(path, new)
            changed.append(rel)

    lock_path = REPO_ROOT / LOCK_REL
    old = _read(lock_path)
    new_head, count = LOCK_VER_RE.subn(
        '"version": "{}"'.format(version), old[:LOCK_HEAD_LIMIT], count=2
    )
    if count != 2:
        raise RuntimeError(f"{LOCK_REL}：头部版本字面量不是 2 处（结构可能变了）")
    new = new_head + old[LOCK_HEAD_LIMIT:]
    if new != old:
        _write(lock_path, new)
        changed.append(LOCK_REL)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="客户端版本号单一真源：校验 / 同步 / 发版")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="校验所有派生字面量（默认）")
    group.add_argument("--sync", action="store_true", help="把派生字面量同步成 version.txt")
    group.add_argument("--bump", metavar="X.Y.Z", help="发版：写入新版本号并同步全部位置")
    args = parser.parse_args(argv)

    if args.bump:
        if not VERSION_RE.match(args.bump):
            parser.error(f"版本号格式非法：{args.bump!r}（应为 X.Y.Z 或 X.Y.Z-beta）")
        _write(VERSION_FILE, args.bump + "\n")
        print(f"version.txt -> {args.bump}")
    version = read_version()

    if args.sync or args.bump:
        changed = sync(version)
        suffix = "：" + "、".join(changed) if changed else ""
        print(f"已同步 {version}：改了 {len(changed)} 个文件{suffix}")
        return 0

    problems = collect_mismatches(version)
    print(f"唯一真源 version.txt = {version}（受控位置 {len(TARGETS) + 1} 个）")
    if problems:
        for item in problems:
            print("  x " + item, file=sys.stderr)
        print("\n版本不一致。发版请用 --bump，修复漂移请用 --sync。", file=sys.stderr)
        return 1
    for rel, _p, _t, _e in TARGETS:
        print(f"  ok {rel}")
    print(f"  ok {LOCK_REL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
