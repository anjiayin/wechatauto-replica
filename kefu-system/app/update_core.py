# -*- coding: utf-8 -*-
"""自动更新的共享纯逻辑（托盘更新器 + 后端 API 共用同一份实现）。

放在版本层随包发布，避免「托盘更新的判定」与「界面更新接口的判定」出现
两套语义。涵盖技术方案 7.1/7.2/7.4/7.5 的纯计算部分：

  - 数字版本比较（禁止字符串比较）
  - manifest Ed25519 验签
  - decide() 三级判定（channel / sequence 防重放 / 强制 vs 可选 /
    ignore：忽略绑定 sequence，重发布自动解除，老记录视为过期）
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
# 「忽略此版本」绑定忽略当时的 manifest sequence；老 DB 没有这个键，
# 新逻辑把「只有版本号、没有 sequence」的记录视为过期（存量自愈）。
KEY_IGNORED_SEQUENCE = "update.ignored_sequence"
# 全量安装包（Inno Setup exe）拉起后、新版本启动自检确认前的「在途」记录。
# 旧逻辑在 Popen 成功那一刻就推进 KEY_LAST_SEQUENCE，安装器被杀软拦停时
# sequence 已被吃掉，客户端再也不会提示升级（现场 1.3.2 事故）。现在改为
# 只记 pending，新版本启动后由 reconcile 确认再推进 last_sequence。
KEY_PENDING_VERSION = "update.pending_version"
KEY_PENDING_SEQUENCE = "update.pending_sequence"


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
def update_available(current: str, m: dict, last_seen: int,
                     channel: str = "stable") -> bool:
    """manifest 上是否存在值得提示的更新（不含强制/忽略分支）。

    与 :func:`decide` 共用 channel / sequence 防重放 / 版本号三段前置判定，
    避免「是否有新版本」和「是否提示」两处规则漂移。
    """
    if str(m.get("channel", "")) != channel:
        return False
    if int(m.get("sequence", 0) or 0) <= int(last_seen or 0):
        return False
    latest = str(m.get("latest", ""))
    if not VERSION_RE.match(latest):
        return False
    return compare_versions(current, latest) < 0


def parse_ignored_sequence(raw: Any) -> int | None:
    """把 settings 里的忽略 sequence 解析为 int；缺失/非法返回 None。

    ``None`` 专指「老记录：只存了版本号没有 sequence」。这类记录在新逻辑下
    一律视为过期，否则现场那台 ``ignored_version=1.2.5`` 的客户端升级后
    仍会被永久屏蔽，必须手改 DB 才能恢复。
    """
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def is_ignored(ignored: str, ignored_sequence: int | None, m: dict) -> bool:
    """用户的「忽略此版本」是否仍然覆盖当前 manifest。

    忽略记录绑定忽略当时的 manifest sequence：只有
    ``manifest.sequence <= ignored_sequence`` 才继续屏蔽。厂商重发布同版本
    号但抬高 sequence 时自动解除忽略；老记录（无 sequence）视为过期。
    """
    latest = str(m.get("latest", ""))
    if not ignored or str(ignored) != latest:
        return False
    seq = parse_ignored_sequence(ignored_sequence)
    if seq is None:
        return False
    return int(m.get("sequence", 0) or 0) <= seq


def decide(current: str, m: dict, ignored: str, last_seen: int,
           channel: str = "stable",
           ignored_sequence: int | None = None) -> str:
    """三级判定：channel / sequence 防重放 / 强制 vs 可选 / ignore。

    ``ignored_sequence`` 为新增的可选参数（默认 None），保持既有 4/5 参数
    调用方（老 updater.pyc、tests）签名兼容；None 表示老式忽略记录，按过期
    处理，让存量客户端自愈。
    """
    if not update_available(current, m, last_seen, channel):
        return "none"                              # 已是最新 / 绝不降级 / 防重放
    min_sup = str(m.get("min_supported", "0.0.0"))
    if compare_versions(current, min_sup) < 0:
        return "forced"                            # 强制更新不受忽略影响
    if is_ignored(ignored, ignored_sequence, m):
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
# 全量安装包（Inno Setup exe）静默重装
# ---------------------------------------------------------------------------
def is_full_package(pkg: dict | None) -> bool:
    """包条目是否为 Inno Setup 全量安装包（type == 'full'，大小写不敏感）。"""
    if not isinstance(pkg, dict):
        return False
    return str(pkg.get("type", "") or "").strip().lower() == "full"


def full_install_confirmed(current: str, pending_version: str) -> bool:
    """启动自检判定「上次全量安装是否真正落地」。

    只有当前运行版本 >= pending 版本才算确认。版本号必须走数字比较，
    禁止字符串比较（1.9 < 1.10）。pending 缺失/非法一律返回 False，
    让客户端保留重试提示，绝不误判成功。
    """
    pending = str(pending_version or "").strip()
    if not pending:
        return False
    try:
        return compare_versions(current, pending) >= 0
    except ValueError:
        return False


def full_install_args(exe: Path, *, close_apps: bool = True) -> list[str]:
    """构造 Inno Setup 静默重装命令行参数列表（不含 exe 自身）。

    返回形如：
    ["/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES",
     "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"]
    close_apps=False 时去掉后两项（测试/特殊场景用）。
    参数顺序固定，便于测试断言。

    ``exe`` 保留在签名里供调用方表达意图并兼容未来按包定制参数；当前
    Inno Setup 开关与具体文件路径无关，故函数体不使用它。
    """
    del exe  # 显式声明未使用，避免静态检查误报
    args = ["/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"]
    if close_apps:
        args.extend(["/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"])
    return args


def launch_full_installer(exe: Path, *, close_apps: bool = True) -> bool:
    """以 detached 子进程启动全量安装包，返回是否成功拉起。

    - exe 必须是已下载且已校验 sha256 的本地文件；不存在返回 False。
    - 使用 subprocess.Popen([str(exe)] + full_install_args(...),
      close_fds=True, stdin/stdout/stderr=DEVNULL,
      creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS)。
    - 绝不调用 wait()/communicate()：安装器会通过 RestartManager 重启本进程，
      等待必然失败（子进程等父进程退出，父进程又在等子进程）。
    - 捕获 OSError 等异常返回 False，不抛出。
    - 返回 True 仅表示进程已拉起，不代表安装成功。

    Windows 上 ``CREATE_NEW_PROCESS_GROUP`` 与 ``DETACHED_PROCESS`` 均存在；
    非 Windows 测试环境用其真实的 Windows 数值兜底，保证 flags 恒非 0，
    便于 monkeypatch 的假 Popen 断言 detached 语义。
    """
    try:
        exe = Path(exe)
        if not exe.is_file():
            return False
        cmd = [str(exe)] + full_install_args(exe, close_apps=close_apps)
        flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                 | getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
        subprocess.Popen(
            cmd, close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    except Exception:                            # noqa: BLE001 - fail-closed
        return False
    return True


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


def public_summary(m: dict, action: str, current: str,
                   ignored: bool = False) -> dict[str, Any]:
    """裁出给前端展示的 manifest 字段（不暴露内部 URL / hash）。

    ``ignored`` 为新增展示字段：仅有新版本但被用户主动忽略时为 True，
    供前端显示「已忽略此版本更新」而不是误导性的「当前已经是最新版本」。
    默认 False 保持旧调用方的返回结构兼容。
    """
    return {
        "action": action,
        "current_version": current,
        "latest": str(m.get("latest", "")),
        "sequence": int(m.get("sequence", 0) or 0),
        "ignored": bool(ignored),
        "notes": str(m.get("notes", ""))[:1000],
        "min_wechat_version": str(m.get("min_wechat_version", "")),
        # 当前交付件未做代码签名；manifest 未显式关闭时也按「需要关杀软」处理。
        "requires_antivirus_off": bool(m.get("requires_antivirus_off", True)),
        "released_at": str(m.get("released_at", "")),
        "package_size": 0,
    }
