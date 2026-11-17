# -*- coding: utf-8 -*-
"""P1 独立更新器（编译成 updater.pyc，由托盘「检查更新」拉起）。

流程（对应技术方案第 7、8 节）：
  1. 拉 OSS manifest.json + .sig，Ed25519 验签（复用 license.py 公钥体系）
  2. 版本判定：channel / sequence 防重放 / 数字版本号 / 强制 vs 可选 / ignore
  3. 关杀软确认页（tkinter，下载前！）：版本号 + 内容 + 大小 + 勾选「已关闭」
  4. 下载 app 增量包到 data/update/*.part → Range 续传 → sha256 强校验
     → 校验失败判定「疑似被安全软件拦截」，停在原版本
  5. 解压到 app/<新版本>/（逐 entry 防路径穿越）→ 完整性自检
  6. 原子切换 app/current junction → 写 current.json → 提示重启
  7. 失败任何一步都不写 current.json，旧版继续可跑

可测试性：decide()/verify_manifest()/safe_extract() 是纯函数或最小 IO，
单元测试在 kefu-system/tests/test_updater.py。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

BUNDLE = Path(__file__).resolve().parent
DATA = BUNDLE / "data"
CURRENT_JSON = BUNDLE / "current.json"
APP_DIR = BUNDLE / "app"

# manifest 地址：构建期注入 KEFU_UPDATE_MANIFEST_URL，未配置则跳过检查（fail-safe）
MANIFEST_URL = os.environ.get("KEFU_UPDATE_MANIFEST_URL", "").strip().rstrip("/")
CHANNEL = os.environ.get("KEFU_UPDATE_CHANNEL", "stable").strip() or "stable"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?$")

# Ed25519 公钥（与 app/license.py 的 PUBLIC_KEYS 保持一致；构建期注入避免维护两份）
# 这里在运行时从 license.py 读取；更新器独立运行时 license.py 在版本层，可 import。
def _public_keys() -> dict[str, str]:
    try:
        from app.license import PUBLIC_KEYS      # type: ignore
        return dict(PUBLIC_KEYS)
    except Exception:                            # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# 版本比较（与 version_sync.compare_versions 同语义，独立实现避免依赖工具脚本）
# ---------------------------------------------------------------------------
def parse_version(text: str) -> tuple[tuple[int, ...], int, str]:
    m = re.match(r"^(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.\-]+))?$", str(text).strip())
    if not m:
        raise ValueError(f"非法版本号：{text}")
    nums = tuple(int(x) for x in m.group(1).split("."))
    pre = m.group(2) or ""
    # 预发布版本序：数字段相同的情况下，正式版 > 预发布版
    stage = 0 if pre else 1
    return nums, stage, pre


def compare_versions(a: str, b: str) -> int:
    na, sa, pa = parse_version(a)
    nb, sb, pb = parse_version(b)
    la, lb = len(na), len(nb)
    n = max(la, lb)
    aa = na + (0,) * (n - la)
    bb = nb + (0,) * (n - lb)
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
def verify_manifest(payload: bytes, sig_doc: dict, public_keys: dict[str, str]) -> tuple[bool, str]:
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


# ---------------------------------------------------------------------------
# 判定（对应方案 7.2）
# ---------------------------------------------------------------------------
def decide(current: str, m: dict, ignored: str, last_seen: int,
           channel: str = CHANNEL) -> str:
    if str(m.get("channel", "")) != channel:
        return "none"
    if int(m.get("sequence", 0)) <= int(last_seen or 0):
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
    for p in m.get("packages", []):
        if p.get("type") != "app":
            continue
        fv = p.get("from_versions")
        if not fv or current in fv:
            return p
    for p in m.get("packages", []):
        if p.get("type") == "full":
            return p
    return None


# ---------------------------------------------------------------------------
# 下载 + 校验
# ---------------------------------------------------------------------------
def download(url: str, dst: Path, expect_sha256: str, expect_size: int,
             progress=None) -> tuple[bool, str]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_suffix(dst.suffix + ".part")
    done = part.stat().st_size if part.is_file() else 0
    headers = {}
    if done > 0:
        headers["Range"] = f"bytes={done}-"
    try:
        req = urllib.request.Request(url, headers=headers)
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
                        progress(done, expect_size)
    except Exception as exc:                      # noqa: BLE001
        return False, f"下载失败：{exc}"
    if expect_size and part.stat().st_size != expect_size:
        return False, f"大小不符：期望 {expect_size}，实际 {part.stat().st_size}"
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
    target = target.resolve()
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


def _rebuild_junction(link: Path, target: Path) -> bool:
    ps = (
        f"$i = Get-Item -LiteralPath '{link}' -Force -ErrorAction SilentlyContinue; "
        f"if ($i) {{ $i.Delete() }}; "
        f"New-Item -ItemType Junction -Path '{link}' -Target '{target}' -ErrorAction Stop | Out-Null"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, text=True)
    return r.returncode == 0


# ---------------------------------------------------------------------------
# 关杀软确认（tkinter）
# ---------------------------------------------------------------------------
def confirm_antivirus(m: dict, pkg: dict, forced: bool) -> bool:
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        return False
    root = tk.Tk()
    root.title("微信客服 · 更新确认")
    root.resizable(False, False)
    notes = str(m.get("notes", ""))[:300]
    size_mb = round(int(pkg.get("size", 0)) / 1024 / 1024, 1)
    txt = (
        f"发现新版本 {m.get('latest')}（当前 {_current_version()}）\n\n"
        f"更新内容：\n{notes}\n\n"
        f"下载大小：约 {size_mb} MB\n"
    )
    if m.get("min_wechat_version"):
        txt += f"需微信版本 ≥ {m['min_wechat_version']}\n"
    txt += (
        "\n⚠ 本程序未做代码签名。更新会写入新的程序文件，"
        "360 / 火绒 / Windows Defender 等安全软件可能拦截。\n"
        "请先关闭安全软件，更新完成并能正常启动后再重新开启。\n"
    )
    ttk.Label(root, text=txt, justify="left", wraplength=460).pack(padx=16, pady=12)
    agreed = tk.BooleanVar(value=False)
    ttk.Checkbutton(root, text="我已关闭安全软件", variable=agreed).pack(padx=16, anchor="w")

    result = {"ok": False}

    def on_continue():
        result["ok"] = bool(agreed.get())
        root.destroy()

    btn_frame = ttk.Frame(root)
    btn_frame.pack(padx=16, pady=12, fill="x")
    cont = ttk.Button(btn_frame, text="继续更新", command=on_continue, state="disabled")
    cont.pack(side="right", padx=4)

    def on_check():
        cont.config(state="normal" if agreed.get() else "disabled")
    agreed.trace_add("write", lambda *_: on_check())

    if not forced:
        ttk.Button(btn_frame, text="稍后", command=root.destroy).pack(side="right", padx=4)
    root.mainloop()
    return result["ok"]


def _current_version() -> str:
    try:
        return json.loads(CURRENT_JSON.read_text(encoding="utf-8"))["version"]
    except Exception:                            # noqa: BLE001
        return "?"


def _state() -> tuple[str, int]:
    """从 settings 表读 ignored / last_seen（更新相关内部键）。"""
    ignored, last_seen = "", 0
    db = DATA / "kefu.sqlite3"
    if db.is_file():
        try:
            import sqlite3
            con = sqlite3.connect(db)
            for key, val in con.execute(
                    "select key,value from settings where key in (?,?)",
                    ("update.ignored_version", "update.last_sequence")):
                if key == "update.ignored_version":
                    ignored = str(val or "")
                else:
                    last_seen = int(val or 0)
            con.close()
        except Exception:                        # noqa: BLE001
            pass
    return ignored, last_seen


def _save_state(ignored: str, last_seen: int) -> None:
    db = DATA / "kefu.sqlite3"
    if not db.is_file():
        return
    try:
        import sqlite3
        con = sqlite3.connect(db)
        con.executemany(
            "insert into settings(key,value) values(?,?) "
            "on conflict(key) do update set value=excluded.value",
            [("update.ignored_version", ignored), ("update.last_sequence", str(last_seen))])
        con.commit()
        con.close()
    except Exception:                            # noqa: BLE001
        pass


def _fetch_manifest() -> tuple[dict | None, dict | None, str]:
    if not MANIFEST_URL:
        return None, None, "未配置更新地址"
    try:
        url = f"{MANIFEST_URL}/manifest.json?t={int(time.time())}"
        with urllib.request.urlopen(url, timeout=20) as r:
            payload = r.read()
        with urllib.request.urlopen(f"{MANIFEST_URL}/manifest.json.sig?t={int(time.time())}",
                                    timeout=20) as r:
            sig_doc = json.loads(r.read().decode("utf-8"))
        m = json.loads(payload.decode("utf-8"))
        ok, reason = verify_manifest(payload, sig_doc, _public_keys())
        if not ok:
            return None, None, f"验签失败：{reason}"
        return m, sig_doc, ""
    except Exception as exc:                     # noqa: BLE001
        return None, None, f"拉取 manifest 失败：{exc}"


def run() -> int:
    m, _sig, err = _fetch_manifest()
    if err:
        print(f"[update] 跳过检查：{err}")
        return 0
    current = _current_version()
    ignored, last_seen = _state()
    action = decide(current, m, ignored, last_seen)
    print(f"[update] current={current} latest={m.get('latest')} action={action}")
    if action == "none":
        return 0
    pkg = pick_package(m, current)
    if not pkg:
        print("[update] manifest 无可用安装包")
        return 2

    if not confirm_antivirus(m, pkg, forced=(action == "forced")):
        if action == "optional":
            _save_state(str(m.get("latest", "")), 0)  # 记 ignored，但不推进 last_seen
            print("[update] 用户取消")
        return 0

    dst = DATA / "update" / f"app-{m['latest']}.zip"
    ok, err = download(str(pkg["url"]), dst, str(pkg.get("sha256", "")),
                       int(pkg.get("size", 0)))
    if not ok:
        print(f"[update] {err}；停在原版本")
        return 3

    ver = str(m["latest"])
    target = APP_DIR / ver
    if target.exists():
        shutil.rmtree(target)
    ok, err = safe_extract(dst, target)
    if not ok:
        print(f"[update] {err}；停在原版本")
        return 4

    # 完整性自检：至少要有 kefu-system/app 目录
    if not (target / "kefu-system" / "app").is_dir():
        print("[update] 解压内容不完整；停在原版本")
        shutil.rmtree(target, ignore_errors=True)
        return 5

    # 原子切换 junction + 写 current.json
    if not _rebuild_junction(APP_DIR / "current", target):
        print("[update] 切换 current junction 失败；停在原版本")
        return 6
    prev = current
    CURRENT_JSON.write_text(json.dumps(
        {"version": ver, "active": ver, "previous": prev}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    _save_state("", int(m.get("sequence", 0)))
    print(f"[update] 已切换到 {ver}，请重启程序生效。")
    return 0


def main() -> None:
    code = run()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    main()
