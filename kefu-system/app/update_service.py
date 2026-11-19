# -*- coding: utf-8 -*-
"""自动更新后端服务：给前端 /api/system/update/* 提供状态机。

与 packaging/updater.py（托盘「检查更新」）共用 app/update_core.py 的判定、
验签、下载、解压逻辑；区别只在确认方式：

  - 托盘更新器：tkinter 弹窗勾选「已关闭安全软件」
  - 界面更新：前端对话框勾选后 POST /download 带 confirmed=true

后端绝不自己弹窗、绝不下载未确认的包。下载/解压/切换全部「失败即停」，
任何异常都不写 current.json，旧版继续可跑（方案第 7/8 节底线）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from app import license as license_mod
from app import update_core as core
from app.config import APP_ROOT, APP_VERSION
from app.db import Database

logger = logging.getLogger(__name__)

# manifest 地址/渠道由启动器注入（构建期写入 start_kefu.bat 或环境变量）
_manifest_url = os.environ.get("KEFU_UPDATE_MANIFEST_URL", "").strip().rstrip("/")
_channel = os.environ.get("KEFU_UPDATE_CHANNEL", "stable").strip() or "stable"


def bundle_root() -> Path | None:
    """定位安装根（三层布局的 bundle）。

    发布态 APP_ROOT = <bundle>/app/current/kefu-system；显式环境变量优先。

    ⚠️ 关键：**不能**用 ``Path.resolve()`` 做结构判定。Windows 上
    ``app/current`` 是目录 junction，``resolve()`` 会跟随它并解析成
    ``app/<版本>``，于是 ``parent.name == "current"`` 永远不成立——所有经
    junction 启动的安装版都会被误判为开发态，download/apply 被 409 拦住。
    这里改用 ``os.path.abspath`` 只做词法归一化（不跟随链接，但会消 ``..``
    并转绝对路径），保留 ``current`` 字面量。

    开发态（APP_ROOT = 仓库/kefu-system）与旧 P0 两层布局仍返回 None，
    接口据此报告「未安装为客户端版本」，避免误操作源码/旧布局目录。
    """
    explicit = os.environ.get("KEFU_BUNDLE_ROOT", "").strip()
    if explicit:
        return Path(explicit)
    # abspath 不跟随 Windows junction / symlink，只做词法归一化；
    # Path.resolve() 会把 current 解析成 1.2.1/1.2.2，正是本次故障根因。
    root = Path(os.path.abspath(str(APP_ROOT)))
    if root.name == "kefu-system" and root.parent.name == "current" \
            and root.parent.parent.name == "app":
        return root.parent.parent.parent
    return None


# ---------------------------------------------------------------------------
# 安装根更新（bundle 根 launcher.pyc / updater.pyc / start_kefu.bat）
# ---------------------------------------------------------------------------
# 白名单：增量包 bundle_root/ 前缀允许写入安装根的文件。与
# packaging/build_release.ROOT_BUNDLE_FILES、packaging/updater.ROOT_BUNDLE_FILES
# 三处必须一字不差（测试锁死）；白名单之外的内容一律拒绝，防止 zip 投毒
# 借 bundle_root 写任意路径（safe_extract 防穿越之后的第二道闸）。
ROOT_BUNDLE_FILES = ("launcher.pyc", "updater.pyc", "start_kefu.bat")
BUNDLE_ROOT_DIR = "bundle_root"
# 根文件替换失败（如被杀软短暂占用）的重试策略：重试后降级为跳过该文件，
# 绝不让升级卡死在旧版（.pyc/.bat 不是可执行映像，运行中的 pythonw 锁的是
# pythonw.exe，正常情况下替换不会被占用）。
_ROOT_COPY_ATTEMPTS = 3
_ROOT_COPY_RETRY_WAIT = 0.3


def install_bundle_root_files(target: Path, bundle: Path) -> None:
    """把解压目录里 ``bundle_root/`` 下的白名单文件写入安装根（bundle 根）。

    语义：
      - 解压目录无 ``bundle_root/`` 子目录 → 直接返回（旧式 zip 向后兼容）；
      - ``bundle_root/`` 内出现白名单之外的任何条目（含子目录）→ 抛
        RuntimeError，由调用方 fail-closed 中止升级；
      - 白名单文件缺失 → 跳过该文件（部分包/旧包兼容，不视为失败）；
      - 单个文件替换持续失败（OSError）→ 重试 ``_ROOT_COPY_ATTEMPTS`` 次后
        打 WARN 降级为跳过该文件，继续升级版本层；
      - 其他异常向上抛：调用方在切 junction 之前调用本函数，任何失败都意味着
        不切 junction、不写 current.json，旧版继续可跑。
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
                    logger.warning(
                        "安装根文件 %s 替换失败（重试 %d 次），跳过该文件继续升级：%s",
                        name, _ROOT_COPY_ATTEMPTS, exc)
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


def repair_bundle_root_files(app_root: Path | None = None,
                             bundle: Path | None = None) -> list[str]:
    """启动自检补写：把版本层遗留的 ``bundle_root/`` 补写到安装根。

    背景（「首跳缺口」）：执行 1.2.3 -> 1.2.4 升级的是客户端上仍在运行的
    旧代码，旧 apply 不认识 ``bundle_root/`` 前缀——那一次升级版本层切到
    1.2.4 了，但 launcher.pyc / updater.pyc / start_kefu.bat 没有被写入
    安装根。新版本层（1.2.4）解压时留下的 ``app/<版本>/bundle_root/`` 会
    一直留在磁盘上，正好作为自检数据源：每次启动检查一次，存在就补写。

    语义：
      - 安装根定位失败（开发态 / 旧布局，``bundle_root()`` 返回 None）→
        直接返回（开发态源码树里没有 bundle 根可写，绝不能动）；
      - 版本层无 ``bundle_root/`` → 直接返回（无首跳缺口，no-op）；
      - 复用 :func:`install_bundle_root_files`：白名单外条目 RuntimeError、
        单文件替换失败重试后降级跳过，语义与升级路径完全一致；
      - 幂等：每次启动都跑。采用「直接覆盖 + sha256 对比决定是否记 INFO」
        而不是「先比再跳过」——三个文件都是 KB 级小文件，copyfile 开销可
        忽略；无条件覆盖还能把历史上半途失败（部分文件被占用没写上）的
        状态收敛到一致，避免比对逻辑本身出 bug 漏补；
      - fail-open：任何异常只打 WARN，绝不阻断后端启动——这不是升级路径，
        下次启动还会再试；
      - 补写成功打 INFO 列出被更新的文件名。launcher.pyc / start_kefu.bat /
        .pyc 不是可执行映像，Windows 只锁 exe，可被运行中进程直接覆盖；
        新代码在下次重启 launcher 后生效。
    """
    try:
        target_bundle = bundle if bundle is not None else bundle_root()
        if target_bundle is None:
            return []                       # 开发态 / 旧布局：无安装根可补写
        # 版本层目录 = APP_ROOT 的父目录（APP_ROOT = <版本层>/kefu-system）。
        # 用 abspath 词法归一化而非 resolve()：junction 启动时 resolve 会把
        # current 解析成版本号，父目录判定失效（见 bundle_root() 的教训）。
        version_layer = Path(os.path.abspath(str(app_root or APP_ROOT))).parent
        if not (version_layer / BUNDLE_ROOT_DIR).is_dir():
            return []                       # 无旧 apply 遗留，no-op
        before = {name: _sha256_file(target_bundle / name)
                  for name in ROOT_BUNDLE_FILES}
        install_bundle_root_files(version_layer, target_bundle)
        updated = [name for name in ROOT_BUNDLE_FILES
                   if _sha256_file(target_bundle / name) != before[name]]
        if updated:
            logger.info(
                "启动自检：已把版本层遗留的 bundle_root/ 补写到安装根，"
                "更新文件：%s（launcher 下次重启后生效）", ", ".join(updated))
        else:
            logger.debug("启动自检：bundle_root/ 遗留目录存在，"
                         "安装根文件已一致，无需补写")
        return updated
    except Exception as exc:                # noqa: BLE001 - fail-open
        logger.warning(
            "启动自检补写安装根文件失败（不阻断启动，下次启动将重试）：%s", exc)
        return []


class UpdateService:
    """更新流程状态机（进程级单例，由 router_update 持有）。"""

    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()
        self._last_manifest: dict[str, Any] | None = None
        # 最近一次 check 看到的 manifest sequence；ignore() 在没有 manifest
        # 上下文时用它绑定忽略记录，避免误绑「上次已安装版本」的 last_seen。
        self._last_check_sequence: int = 0
        self._job: dict[str, Any] = {
            "state": "idle", "done": 0, "total": 0, "message": "",
            "version": "", "action": "none",
        }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _keys(self) -> dict[str, str]:
        """验签公钥：与授权共用 license.PUBLIC_KEYS，不维护第二份。"""
        try:
            return dict(license_mod.PUBLIC_KEYS)
        except Exception:                        # noqa: BLE001
            return {}

    def _manifest(self) -> tuple[dict | None, str]:
        return core.fetch_manifest(_manifest_url, self._keys())

    # ------------------------------------------------------------------
    # 检查
    # ------------------------------------------------------------------
    def check(self) -> dict[str, Any]:
        """拉 manifest → 验签 → 判定；不发任何网络请求之外的副作用。"""
        if not _manifest_url:
            return {"ok": True, "action": "none", "current_version": APP_VERSION,
                    "latest": "", "reason": "未配置更新地址"}
        m, err = self._manifest()
        if err:
            # 更新检查失败绝不能阻断业务；把原因回给前端但不弹错误框。
            logger.info("检查更新跳过：%s", err)
            return {"ok": True, "action": "none", "current_version": APP_VERSION,
                    "latest": "", "reason": err}
        with self._lock:
            ignored = self.db.get_setting(core.KEY_IGNORED_VERSION, "")
            ignored_seq = core.parse_ignored_sequence(
                self.db.get_setting(core.KEY_IGNORED_SEQUENCE, ""))
            last_seen = self.db.get_int(core.KEY_LAST_SEQUENCE, 0)
        action = core.decide(APP_VERSION, m, ignored, last_seen,
                             _channel, ignored_seq)
        # 仅当「确实有新版本、且是被用户的忽略决定挡住」时标 ignored，
        # 避免 sequence 防重放/已是最新/强制更新等场景误报「已忽略」。
        ignored_flag = bool(
            action == "none"
            and core.update_available(APP_VERSION, m, last_seen, _channel)
            and core.is_ignored(ignored, ignored_seq, m))
        summary = core.public_summary(m, action, APP_VERSION,
                                      ignored=ignored_flag)
        pkg = core.pick_package(m, APP_VERSION)
        if pkg and action != "none":
            summary["package_size"] = int(pkg.get("size", 0) or 0)
            summary["package_type"] = str(pkg.get("type", ""))
        summary["ok"] = True
        with self._lock:
            self._last_manifest = m if action != "none" else None
            self._last_check_sequence = int(summary.get("sequence", 0) or 0)
        logger.info("检查更新：current=%s latest=%s action=%s ignored=%s",
                    APP_VERSION, summary.get("latest"), action, ignored_flag)
        return summary

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._job)

    def download(self, confirmed: bool) -> tuple[int, dict[str, Any]]:
        """启动后台下载。必须在前端勾选「已关闭安全软件」后调用。"""
        if not confirmed:
            return 400, {"ok": False, "detail": "请先确认已关闭安全软件"}
        with self._lock:
            m = self._last_manifest
            if m is None:
                return 409, {"ok": False,
                             "detail": "请先检查更新；若已检查请重新检查后再试"}
            if self._job["state"] in ("downloading", "applying"):
                return 409, {"ok": False, "detail": "正在下载/安装，请勿重复操作"}
            pkg = core.pick_package(m, APP_VERSION)
            if not pkg:
                return 409, {"ok": False, "detail": "manifest 无可用安装包"}
            bundle = bundle_root()
            if bundle is None:
                return 409, {"ok": False, "detail": "当前不是客户端安装版，无法执行更新"}
            version = str(m.get("latest", ""))
            self._job = {"state": "downloading", "done": 0,
                         "total": int(pkg.get("size", 0) or 0), "message": "",
                         "version": version, "action": "optional"}
            dst = bundle / "data" / "update" / f"app-{version}.zip"
            url, sha, size = str(pkg.get("url", "")), str(pkg.get("sha256", "")), int(pkg.get("size", 0) or 0)

        def _work() -> None:
            def _progress(done: int, total: int) -> None:
                with self._lock:
                    self._job["done"] = done
                    self._job["total"] = total

            ok, err = core.download(url, dst, sha, size, _progress)
            with self._lock:
                if ok:
                    self._job.update({"state": "done", "done": size,
                                      "total": size, "message": "下载完成"})
                else:
                    # 校验失败＝疑似杀软拦截，停在原版本，绝不进入解压。
                    self._job.update({"state": "error", "message": err})

        threading.Thread(target=_work, name="kefu-update-download",
                         daemon=True).start()
        return 202, {"ok": True, "state": "downloading", "version": version}

    # ------------------------------------------------------------------
    # 应用（解压 + 切 junction + 写 current.json）
    # ------------------------------------------------------------------
    def apply(self) -> tuple[int, dict[str, Any]]:
        with self._lock:
            if self._job["state"] != "done":
                return 409, {"ok": False, "detail": "没有已校验的更新包，请先下载"}
            m = self._last_manifest
            if m is None:
                return 409, {"ok": False, "detail": "更新信息已失效，请重新检查"}
            version = self._job["version"]
            if not core.VERSION_RE.match(version):
                return 409, {"ok": False, "detail": "版本号非法，已中止安装"}
            bundle = bundle_root()
            if bundle is None:
                return 409, {"ok": False, "detail": "当前不是客户端安装版，无法执行更新"}
            self._job["state"] = "applying"
            dst = bundle / "data" / "update" / f"app-{version}.zip"

        try:
            # 落地前再验一次 manifest，防止检查后被替换（OSS 公共读也要防投毒）。
            fresh, err = self._manifest()
            if err or fresh is None or str(fresh.get("latest", "")) != version:
                raise RuntimeError(f"更新信息已变化，已中止：{err or '版本不一致'}")
            target = bundle / "app" / version
            if target.exists():
                shutil.rmtree(target)
            ok, err = core.safe_extract(dst, target)
            if not ok:
                raise RuntimeError(err)
            if not (target / "kefu-system" / "app").is_dir():
                shutil.rmtree(target, ignore_errors=True)
                raise RuntimeError("解压内容不完整（缺少后端代码）")
            # 安装根更新：在切 junction 之前把 bundle_root/ 白名单文件写入
            # bundle 根。本阶段任何异常 → 清理 target、不切 junction、
            # 不写 current.json，旧版继续可跑（fail-closed）。
            try:
                install_bundle_root_files(target, bundle)
            except Exception:
                shutil.rmtree(target, ignore_errors=True)
                raise
            ok, err = core.rebuild_junction(bundle / "app" / "current", target)
            if not ok:
                raise RuntimeError(err)
            prev = core.read_current_version(bundle)
            (bundle / "current.json").write_text(json.dumps(
                {"version": version, "active": version, "previous": prev},
                ensure_ascii=False, indent=2), encoding="utf-8")
            with self._lock:
                self.db.set_setting(core.KEY_LAST_SEQUENCE,
                                    str(int(fresh.get("sequence", 0) or 0)))
                self.db.set_setting(core.KEY_IGNORED_VERSION, "")
                self.db.set_setting(core.KEY_IGNORED_SEQUENCE, "")
                self._job.update({"state": "applied", "message": "安装完成，请重启"})
        except Exception as exc:                  # noqa: BLE001
            logger.warning("更新应用失败：%s", exc)
            with self._lock:
                self._job.update({"state": "error", "message": str(exc)})
            return 500, {"ok": False, "detail": f"安装失败：{exc}（已停在原版本）"}
        return 200, {"ok": True, "need_restart": True, "version": version}

    def ignore(self, version: str) -> dict[str, Any]:
        """忽略当前检查到的版本，并记录其 manifest sequence。

        只有厂商用同一版本号、更高 sequence 重发布时才会重新提示；老数据
        只有版本号没有 sequence，会被 :func:`app.update_core.is_ignored`
        视为过期，现场机器无需清库即可自愈。
        """
        with self._lock:
            m = self._last_manifest
            seq = int(m.get("sequence", 0) or 0) if m else self._last_check_sequence
            target = str(version or "")
            self.db.set_setting(core.KEY_IGNORED_VERSION, target)
            self.db.set_setting(core.KEY_IGNORED_SEQUENCE,
                                str(seq) if target else "")
            self._last_manifest = None
            self._job = {"state": "idle", "done": 0, "total": 0,
                         "message": "", "version": "", "action": "none"}
        return {"ok": True, "ignored": target}

    def unignore(self) -> dict[str, Any]:
        """清除「忽略此版本」记录，使下一次 check 恢复正常判定。"""
        with self._lock:
            self.db.set_setting(core.KEY_IGNORED_VERSION, "")
            self.db.set_setting(core.KEY_IGNORED_SEQUENCE, "")
        return {"ok": True, "ignored": ""}
