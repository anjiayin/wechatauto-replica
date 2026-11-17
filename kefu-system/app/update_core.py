# -*- coding: utf-8 -*-
"""自动更新的共享纯逻辑（托盘更新器 + 后端 API 共用同一份实现）。

放在版本层随包发布，避免「托盘更新的判定」与「界面更新接口的判定」出现
两套语义。涵盖技术方案 7.1/7.2/7.4/7.5 的纯计算部分：

  - 数字版本比较（禁止字符串比较）
  - manifest Ed25519 验签
  - decide() 三级判定（channel / sequence 防重放 / 强制 vs 可选 / ignore）
  - 选包、Range 下载 + SHA256、防路径穿越解压

网络与磁盘的外部状态（current.json / settings 表 / tkinter 确认 / junction
切换）留给调用方：packaging/updater.py 与 app/update_service.py 各自负责。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?$")

# settings 表里的更新内部键（后端与独立更新器必须一致）
KEY_IGNORED_VERSION = "update.ignored_version"
KEY_LAST_SEQUENCE = "update.last_sequence"


# ---------------------------------------------------------------------------
# 版本比较（与 version_sync.compare_versions 同语义）
# ---------------------------------------------------------------------------
def parse_version(text: str) -> tuple[tuple[int, ...], int, str]:
    m = re.match(r"^(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.\-]+))?$", str(text).strip())
    if not m:
        raise ValueError(f"非法版本号：{text}")
    nums = tuple(int(x) for x in m.group(1).split("."))
    pre = m.group(2) or ""
    # 数字段相同：正式版 > 预发布版
    stage = 0 if pre else 1
    return nums, stage, pre


def compare_versions(a: str, b: str) -> int:
    na, sa, pa = parse_version(a)
    nb, sb, pb = parse_version(b)
    n = max(len(na), len(nb))
    aa = na + (0,) * (n - len(na))
    bb = nb + (0,) * (n - len(nb))
    if aa != bb:
        return -1 if aa < bb else 1
    if sa != sb:
        return -1 if sa < sb else 1
    if pa == pb:
        return 0
    return -1 if pa < pb else 1


# ---------------------------------------------------------------------------
# 验签
# ---------------------------------------------------------------------------
def verify_manifest(payload: bytes, sig_doc: dict,
                    public_keys: dict[str, str]) -> tuple[bool, str]:
    """验 manifest 的 Ed25519 签名。sig_doc = {"key_id","signature"}。"""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return False, "缺少 cryptography，无法验签"
    kid = str(sig_doc.get("key_id", ""))
    sig_b64 = str(sig_doc.get("signature", ""))
    pub_b64 = public_keys.get(kid)
    if not pub_b64:
        return False, f"未知签名公钥 key_id={kid!r}"
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))
        pub.verify(base64.b64decode(sig_b64), payload)
        return True, ""
    except Exception as exc:                      # noqa: BLE001
        return False, f"签名验证失败：{exc}"


def fetch_manifest(url: str, public_keys: dict[str, str],
                   timeout: float = 20.0) -> tuple[dict | None, str]:
    """拉取并验签 manifest；返回 (manifest, 错误)。验签失败一律不返回内容。

    URL 必须带 `?t=<时间戳>` 破 CDN 缓存；签名文件与 manifest 成对读取。
    """
    base = str(url or "").strip().rstrip("/")
    if not base:
        return None, "未配置更新地址"
    try:
        ts = int(time.time())
        with urllib.request.urlopen(f"{base}/manifest.json?t={ts}",
                                    timeout=timeout) as r:
            payload = r.read()
        with urllib.request.urlopen(f"{base}/manifest.json.sig?t={ts}",
                                    timeout=timeout) as r:
            sig_doc = json.loads(r.read().decode("utf-8"))
        ok, reason = verify_manifest(payload, sig_doc, public_keys)
        if not ok:
            return None, f"验签失败：{reason}"
        m = json.loads(payload.decode("utf-8"))
        if not isinstance(m, dict):
            return None, "manifest 格式非法"
        return m, ""
    except Exception as exc:                      # noqa: BLE001
        return None, f"拉取 manifest 失败：{exc}"


# ---------------------------------------------------------------------------
# 判定（方案 7.2）
# ---------------------------------------------------------------------------
def decide(current: str, m: dict, ignored: str, last_seen: int,
           channel: str = "stable") -> str:
    if str(m.get("channel", "")) != channel:
        return "none"
    if int(m.get("sequence", 0) or 0) <= int(last_seen or 0):
        return "none"
    latest = str(m.get("latest", ""))
    if not VERSION_RE.match(latest):
        return "none"
    if compare_versions(current, latest) >= 0:
        return "none"                              # 已是最新 / 绝不降级
    min_sup = str(m.get("min_supported", "0.0.0"))
    if compare_versions(current, min_sup) < 0:
        return "forced"
    if ignored == latest:
        return "none"
    return "optional"


def pick_package(m: dict, current: str) -> dict | None:
    """选 app 增量包（from_versions 命中当前版本）；否则退回 full 包。"""
    for p in m.get("packages", []) or []:
        if p.get("type") != "app":
            continue
        fv = p.get("from_versions")
        if not fv or current in fv:
            return p
    for p in m.get("packages", []) or []:
        if p.get("type") == "full":
            return p
    return None


# ---------------------------------------------------------------------------
# 下载 + 校验
# ---------------------------------------------------------------------------
def download(url: str, dst: Path, expect_sha256: str, expect_size: int,
             progress: Callable[[int, int], None] | None = None) -> tuple[bool, str]:
    """Range 断点续传 → 大小/SHA256 强校验 → 原子改名。

    progress(done, total) 每 1MB 回调一次，供前端轮询下载进度。
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_suffix(dst.suffix + ".part")
    done = part.stat().st_size if part.is_file() else 0
    headers = {}
    if done > 0:
        headers["Range"] = f"bytes={done}-"
    try:
        req = urllib.request.Request(str(url), headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            mode = "ab" if (done > 0 and resp.status == 206) else "wb"
            if mode == "wb":
                done = 0
            with open(part, mode) as fh:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, int(expect_size or 0))
    except Exception as exc:                      # noqa: BLE001
        return False, f"下载失败：{exc}"
    actual = part.stat().st_size
    if expect_size and actual != int(expect_size):
        return False, f"大小不符：期望 {expect_size}，实际 {actual}"
    h = hashlib.sha256()
    with open(part, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    if h.hexdigest().lower() != str(expect_sha256).lower():
        return False, "SHA256 校验失败（疑似被安全软件拦截或篡改）"
    os.replace(part, dst)
    return True, ""


def safe_extract(zip_path: Path, target: Path) -> tuple[bool, str]:
    """解压并防路径穿越；返回 (成功, 错误)。"""
    target = Path(target).resolve()
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                dest = (target / name).resolve()
                if not (dest == target or target in dest.parents):
                    return False, f"路径穿越：{info.filename}"
            zf.extractall(target)
        return True, ""
    except Exception as exc:                      # noqa: BLE001
        return False, f"解压失败：{exc}"


def rebuild_junction(link: Path, target: Path) -> tuple[bool, str]:
    """原子重建 app/current junction（两端共用，不能跟随删除目标目录）。

    用 .NET DirectoryInfo.Delete() 只移除链接本身；New-Item 建新链接。
    任一步失败都返回错误，由调用方决定「不写 current.json、停在旧版」。
    """
    ps = (
        f"$i = Get-Item -LiteralPath '{link}' -Force -ErrorAction SilentlyContinue; "
        f"if ($i) {{ $i.Delete() }}; "
        f"New-Item -ItemType Junction -Path '{link}' -Target '{target}' "
        f"-ErrorAction Stop | Out-Null"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=30)
    except Exception as exc:                      # noqa: BLE001
        return False, f"切换 junction 失败：{exc}"
    if r.returncode != 0:
        return False, f"切换 junction 失败：{r.stderr.strip()}"
    return True, ""


def read_current_version(bundle: Path) -> str:
    try:
        return str(json.loads((Path(bundle) / "current.json").read_text(
            encoding="utf-8"))["version"])
    except Exception:                            # noqa: BLE001
        return "?"


def public_summary(m: dict, action: str, current: str) -> dict[str, Any]:
    """裁出给前端展示的 manifest 字段（不暴露内部 URL / hash）。"""
    return {
        "action": action,
        "current_version": current,
        "latest": str(m.get("latest", "")),
        "sequence": int(m.get("sequence", 0) or 0),
        "notes": str(m.get("notes", ""))[:1000],
        "min_wechat_version": str(m.get("min_wechat_version", "")),
        # 当前交付件未做代码签名；manifest 未显式关闭时也按「需要关杀软」处理。
        "requires_antivirus_off": bool(m.get("requires_antivirus_off", True)),
        "released_at": str(m.get("released_at", "")),
        "package_size": 0,
    }
