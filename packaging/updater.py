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

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# 判定/验签/下载/解压的纯逻辑在版本层 app/update_core.py，与后端更新接口
# 共用一份实现。发布态 embed 的 ._pth 已含 app/current/kefu-system；开发态
# （pytest 用 importlib 从 packaging/ 加载）补一次 sys.path 即可。
try:
    from app.update_core import (
        KEY_IGNORED_VERSION, KEY_LAST_SEQUENCE, VERSION_RE,
        compare_versions, decide as _core_decide, download, fetch_manifest,
        parse_version, pick_package, read_current_version, rebuild_junction,
        safe_extract, verify_manifest,
    )
except ModuleNotFoundError:                      # pragma: no cover - 开发态
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "kefu-system"))
    from app.update_core import (                 # type: ignore[no-redef]
        KEY_IGNORED_VERSION, KEY_LAST_SEQUENCE, VERSION_RE,
        compare_versions, decide as _core_decide, download, fetch_manifest,
        parse_version, pick_package, read_current_version, rebuild_junction,
        safe_extract, verify_manifest,
    )

BUNDLE_FILE = Path(__file__).resolve()


def resolve_bundle(explicit: str = "") -> Path:
    """定位安装根：launcher 注入的 KEFU_BUNDLE_ROOT 优先，否则按脚本位置。

    与后端 update_service.bundle_root() 保持同一优先级顺序。updater.pyc 位于
    bundle 根（不经过 app/current junction），脚本自身位置通常已是安装根；
    显式环境变量让托盘与界面两条更新链路使用同一个权威来源。
    """
    text = str(explicit or "").strip() or os.environ.get("KEFU_BUNDLE_ROOT", "").strip()
    return Path(text) if text else BUNDLE_FILE.parent


BUNDLE = resolve_bundle()
DATA = BUNDLE / "data"
CURRENT_JSON = BUNDLE / "current.json"
APP_DIR = BUNDLE / "app"

# manifest 地址：构建期注入 KEFU_UPDATE_MANIFEST_URL，未配置则跳过检查（fail-safe）
MANIFEST_URL = os.environ.get("KEFU_UPDATE_MANIFEST_URL", "").strip().rstrip("/")
CHANNEL = os.environ.get("KEFU_UPDATE_CHANNEL", "stable").strip() or "stable"


def decide(current: str, m: dict, ignored: str, last_seen: int,
           channel: str = CHANNEL) -> str:
    """保留本脚本的 channel 默认值语义，判定实现走共享 core。"""
    return _core_decide(current, m, ignored, last_seen, channel)


# Ed25519 公钥（与 app/license.py 的 PUBLIC_KEYS 保持一致；构建期注入避免维护两份）
# 这里在运行时从 license.py 读取；更新器独立运行时 license.py 在版本层，可 import。
def _public_keys() -> dict[str, str]:
    try:
        from app.license import PUBLIC_KEYS      # type: ignore
        return dict(PUBLIC_KEYS)
    except Exception:                            # noqa: BLE001
        return {}


def _rebuild_junction(link: Path, target: Path) -> bool:
    ok, _err = rebuild_junction(link, target)
    return ok


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
    return read_current_version(BUNDLE)


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
                    (KEY_IGNORED_VERSION, KEY_LAST_SEQUENCE)):
                if key == KEY_IGNORED_VERSION:
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
            [(KEY_IGNORED_VERSION, ignored), (KEY_LAST_SEQUENCE, str(last_seen))])
        con.commit()
        con.close()
    except Exception:                            # noqa: BLE001
        pass


def _fetch_manifest() -> tuple[dict | None, dict | None, str]:
    m, err = fetch_manifest(MANIFEST_URL, _public_keys())
    return m, None, err


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
