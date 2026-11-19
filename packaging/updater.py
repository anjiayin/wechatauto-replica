# -*- coding: utf-8 -*-
"""P1 独立更新器（编译成 updater.pyc，由托盘「检查更新」拉起）。

流程（对应技术方案第 7、8 节）：
  1. 拉 OSS manifest.json + .sig，Ed25519 验签（复用 license.py 公钥体系）
  2. 版本判定：channel / sequence 防重放 / 数字版本号 / 强制 vs 可选 / ignore
  3. 关杀软确认页（tkinter，下载前！）：版本号 + 内容 + 大小 + 勾选「已关闭」
  4. 下载 app 增量包到 data/update/*.part → Range 续传 → sha256 强校验
     → 校验失败判定「疑似被安全软件拦截」，停在原版本
  5. 解压到 app/<新版本>/（逐 entry 防路径穿越）→ 完整性自检
     → bundle_root/ 白名单文件写入安装根（launcher.pyc / updater.pyc /
     start_kefu.bat；白名单外拒绝，替换失败重试后降级跳过）
  6. 原子切换 app/current junction → 写 current.json → 提示重启
  7. 失败任何一步都不写 current.json，旧版继续可跑

可测试性：decide()/verify_manifest()/safe_extract() 是纯函数或最小 IO，
单元测试在 kefu-system/tests/test_updater.py。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# 判定/验签/下载/解压的纯逻辑在版本层 app/update_core.py，与后端更新接口
# 共用一份实现。发布态 embed 的 ._pth 已含 app/current/kefu-system；开发态
# （pytest 用 importlib 从 packaging/ 加载）补一次 sys.path 即可。
try:
    from app.update_core import (
        KEY_IGNORED_VERSION, KEY_IGNORED_SEQUENCE, KEY_LAST_SEQUENCE, VERSION_RE,
        compare_versions, decide as _core_decide, download, fetch_manifest,
        parse_ignored_sequence, parse_version, pick_package,
        read_current_version, rebuild_junction, safe_extract, verify_manifest,
    )
except ModuleNotFoundError:                      # pragma: no cover - 开发态
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "kefu-system"))
    from app.update_core import (                 # type: ignore[no-redef]
        KEY_IGNORED_VERSION, KEY_IGNORED_SEQUENCE, KEY_LAST_SEQUENCE, VERSION_RE,
        compare_versions, decide as _core_decide, download, fetch_manifest,
        parse_ignored_sequence, parse_version, pick_package,
        read_current_version, rebuild_junction, safe_extract, verify_manifest,
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
           channel: str = CHANNEL,
           ignored_sequence: int | None = None) -> str:
    """保留本脚本的 channel 默认值语义，判定实现走共享 core。

    ``ignored_sequence`` 为新增可选参数：None 表示老式「只有版本号」的
    忽略记录，按过期处理，确保存量客户端升级后能重新看到重发布版本。
    """
    return _core_decide(current, m, ignored, last_seen, channel,
                        ignored_sequence)


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
# 安装根更新（bundle 根 launcher.pyc / updater.pyc / start_kefu.bat）
# ---------------------------------------------------------------------------
# 白名单：与 app/update_service.ROOT_BUNDLE_FILES、
# packaging/build_release.ROOT_BUNDLE_FILES 三处必须一字不差（测试锁死）。
# 独立更新器必须自带这份实现（不新增对 app.update_core 的符号依赖）：
# 历史已部署的 updater.pyc 从旧版本层 import update_core，旧 core 里不存在
# 新增符号，顶层 from-import 会直接 ImportError 炸掉更新器。
ROOT_BUNDLE_FILES = ("launcher.pyc", "updater.pyc", "start_kefu.bat")
BUNDLE_ROOT_DIR = "bundle_root"
_ROOT_COPY_ATTEMPTS = 3
_ROOT_COPY_RETRY_WAIT = 0.3


def install_bundle_root_files(target: Path, bundle: Path) -> None:
    """把解压目录里 ``bundle_root/`` 下的白名单文件写入安装根（bundle 根）。

    与 app/update_service.install_bundle_root_files 同一实现、同一语义：
      - 解压目录无 ``bundle_root/`` 子目录 → 直接返回（旧式 zip 向后兼容）；
      - ``bundle_root/`` 内出现白名单之外的任何条目（含子目录）→ 抛
        RuntimeError，由调用方 fail-closed 中止升级；
      - 白名单文件缺失 → 跳过该文件（部分包/旧包兼容，不视为失败）；
      - 单个文件替换持续失败（OSError）→ 重试 ``_ROOT_COPY_ATTEMPTS`` 次后
        打 WARN 降级为跳过该文件，继续升级版本层，绝不让客户卡死在旧版
        （.pyc/.bat 不是可执行映像，运行中的 pythonw 锁的是 pythonw.exe）。
    """
    src_dir = target / BUNDLE_ROOT_DIR
    if not src_dir.is_dir():
        return
    for entry in src_dir.iterdir():
        if entry.name not in ROOT_BUNDLE_FILES:
            raise RuntimeError(
                f"更新包 bundle_root 含白名单外条目，已中止安装：{entry.name}")
    for name in ROOT_BUNDLE_FILES:
        src = src_dir / name
        if not src.is_file():
            continue
        dst = bundle / name
        for attempt in range(1, _ROOT_COPY_ATTEMPTS + 1):
            try:
                shutil.copyfile(src, dst)
                break
            except OSError as exc:
                if attempt == _ROOT_COPY_ATTEMPTS:
                    print(f"[update][WARN] 安装根文件 {name} 替换失败"
                          f"（重试 {_ROOT_COPY_ATTEMPTS} 次），"
                          f"跳过该文件继续升级：{exc}")
                else:
                    time.sleep(_ROOT_COPY_RETRY_WAIT)


def _sha256_file(path: Path) -> str | None:
    """计算文件 sha256；文件不存在 / 不可读返回 None（用于前后对比）。"""
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def repair_bundle_root_files() -> list[str]:
    """入口自检补写：把版本层遗留的 ``bundle_root/`` 补写到安装根。

    与 app/update_service.repair_bundle_root_files 同一语义（托盘场景：
    客户只从托盘触发更新、后端可能长期不重启，靠 run() 入口补一次）。
    数据源是旧 apply 解压遗留的 ``app/<版本>/bundle_root/``——由
    current.json 的 version 定位版本层目录；current.json 缺失/损坏时
    read_current_version 返回 "?"，对应目录不存在 → 安全 no-op。

      - 版本层无 ``bundle_root/`` → 直接返回（no-op）；
      - 复用 install_bundle_root_files：白名单外 RuntimeError、单文件
        替换失败重试后降级跳过，与升级路径完全一致；
      - 幂等：每次 run() 都跑，直接覆盖 + sha256 对比决定是否打印
        INFO（理由同后端：小文件覆盖开销可忽略，且能收敛半途失败状态）；
      - fail-open：任何异常只打 WARN，绝不阻断本次更新流程。
    """
    try:
        version_layer = APP_DIR / read_current_version(BUNDLE)
        if not (version_layer / BUNDLE_ROOT_DIR).is_dir():
            return []                       # 无旧 apply 遗留，no-op
        before = {name: _sha256_file(BUNDLE / name)
                  for name in ROOT_BUNDLE_FILES}
        install_bundle_root_files(version_layer, BUNDLE)
        updated = [name for name in ROOT_BUNDLE_FILES
                   if _sha256_file(BUNDLE / name) != before[name]]
        if updated:
            print(f"[update] 启动自检：已把版本层遗留的 bundle_root/ 补写到"
                  f"安装根，更新文件：{', '.join(updated)}"
                  f"（launcher 下次重启后生效）")
        return updated
    except Exception as exc:                # noqa: BLE001 - fail-open
        print(f"[update][WARN] 启动自检补写安装根文件失败"
              f"（不阻断本次更新，下次重试）：{exc}")
        return []


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


def _ignored_sequence() -> int | None:
    """从 settings 表读「忽略时的 manifest sequence」；老库无此键返回 None。"""
    db = DATA / "kefu.sqlite3"
    if not db.is_file():
        return None
    try:
        import sqlite3
        con = sqlite3.connect(db)
        try:
            row = con.execute(
                "select value from settings where key=?",
                (KEY_IGNORED_SEQUENCE,)).fetchone()
        finally:
            con.close()
        return parse_ignored_sequence(row[0] if row else "")
    except Exception:                            # noqa: BLE001
        return None


def _save_state(ignored: str, last_seen: int,
                ignored_sequence: int | None = None) -> None:
    """写回更新状态；ignored 为空时同时清掉忽略 sequence。"""
    db = DATA / "kefu.sqlite3"
    if not db.is_file():
        return
    seq = "" if ignored_sequence is None else str(int(ignored_sequence))
    try:
        import sqlite3
        con = sqlite3.connect(db)
        con.executemany(
            "insert into settings(key,value) values(?,?) "
            "on conflict(key) do update set value=excluded.value",
            [(KEY_IGNORED_VERSION, ignored), (KEY_IGNORED_SEQUENCE, seq),
             (KEY_LAST_SEQUENCE, str(last_seen))])
        con.commit()
        con.close()
    except Exception:                            # noqa: BLE001
        pass


def _fetch_manifest() -> tuple[dict | None, dict | None, str]:
    m, err = fetch_manifest(MANIFEST_URL, _public_keys())
    return m, None, err


def run() -> int:
    # 首跳缺口自检：旧版升级遗留的版本层 bundle_root/ 在入口补写一次
    # （幂等 + fail-open，见 repair_bundle_root_files 文档）。
    repair_bundle_root_files()
    m, _sig, err = _fetch_manifest()
    if err:
        print(f"[update] 跳过检查：{err}")
        return 0
    current = _current_version()
    ignored, last_seen = _state()
    ignored_sequence = _ignored_sequence()
    action = decide(current, m, ignored, last_seen,
                    ignored_sequence=ignored_sequence)
    print(f"[update] current={current} latest={m.get('latest')} action={action}")
    if action == "none":
        return 0
    pkg = pick_package(m, current)
    if not pkg:
        print("[update] manifest 无可用安装包")
        return 2

    if not confirm_antivirus(m, pkg, forced=(action == "forced")):
        if action == "optional":
            # 记 ignored 并绑定本次 manifest sequence；不推进 last_seen。
            # 厂商同版本号重发布（sequence 增大）时会自动重新提示。
            _save_state(str(m.get("latest", "")), 0,
                        int(m.get("sequence", 0) or 0))
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

    # 安装根更新：在切 junction 之前把 bundle_root/ 白名单文件写入安装根。
    # 白名单外条目 → 清理 target、不切 junction、不写 current.json，旧版继续可跑。
    try:
        install_bundle_root_files(target, BUNDLE)
    except RuntimeError as exc:
        print(f"[update] {exc}；停在原版本")
        shutil.rmtree(target, ignore_errors=True)
        return 7

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
