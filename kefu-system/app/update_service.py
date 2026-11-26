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


# 启动自检的并发保护（Database 自身不是线程安全的；主流程用 self._lock，
# 这里没有实例，单独用一把模块锁）。
_RECONCILE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 增量升级落地后，请托盘启动器把后端重启一次
# ---------------------------------------------------------------------------
# 为什么用文件不用接口：托盘启动器（packaging/launcher.py）是独立进程、且刻意
# 不 import 业务模块。它每 2 秒看一次 data/update_handoff.json，看到就把后端优雅
# 重启（CTRL_BREAK 停 → 重新 spawn → 等就绪 → 重开页面）。写文件的正是本次升级前
# 的那个后端，它随后就被重启掉，所以「谁写谁不重启」不会自杀。
def _handoff_data_dir() -> Path | None:
    """接力文件目录 = 安装根 data/（与启动器的 DATA 同一个）。

    返回 None（源码运行、找不到安装根）时整条链路自动禁用：那种环境本来也没有
    启动器进程可以请求重启。
    """
    bundle = bundle_root()
    if bundle is None:
        return None
    return bundle / "data"


def _clear_handoff_at_startup() -> None:
    """清掉可能残留的接力文件，两条理由：

    1) 本次重启已经消费过这个请求，重复消费会重启刚起来的自己；
    2) 接力文件写在**旧**版本落地时，万一那次 apply 其实没成功（junction 没切、
       current.json 没写），残留文件会把后来正在下载全量包的后端无辜杀掉。
    """
    try:
        core.clear_handoff(_handoff_data_dir())
    except Exception:                                  # 清不掉也不能挡住启动
        pass


def reconcile_full_install(db: Database, current_version: str = APP_VERSION) -> str:
    """启动自检：把「已确认落地」的全量安装从 pending 推进为 last_sequence。

    full 包由 Inno Setup 自己覆盖版本层并重启进程，客户端拉起安装器后不能
    wait()，也无法当场知道安装是否成功。旧逻辑把「Popen 成功」当成安装成功
    并推进 sequence，杀软一拦就永久吞掉这次更新（现场 1.3.2 事故）。

    现在 full 分支只写 ``update.pending_version/sequence``；新版本启动后调用
    本函数：运行版本 >= pending 版本才算成功，写入 last_sequence 并清 pending；
    版本没变则保留 pending 与原有 last_sequence，下一次 check() 会继续提示，
    用户可以重试安装（fail-closed，绝不吞提示）。

    返回 ``none``（没有在途安装）/ ``confirmed``（已确认）/ ``pending``
    （尚未落地，保留重试）。任何异常只记 WARN，绝不阻断后端启动。
    """
    # 启动自检顺带消费掉上一版遗留的「请重启后端」请求（见 _clear_handoff_at_startup）。
    _clear_handoff_at_startup()
    try:
        pending = str(db.get_setting(core.KEY_PENDING_VERSION, "") or "").strip()
        if not pending:
            return "none"
        seq = int(db.get_int(core.KEY_PENDING_SEQUENCE, 0) or 0)
        if not core.full_install_confirmed(current_version, pending):
            bundle = bundle_root()
            hint = ""
            if bundle is not None and core.VERSION_RE.match(pending):
                hint = "；安装器日志 " + str(core.full_install_log_path(
                    bundle / "data" / "update" / f"full-{pending}.exe"))
            logger.warning(
                "启动自检：上次全量安装 %s 尚未生效（当前版本 %s），"
                "保留更新提示以便重试%s", pending, current_version, hint)
            return "pending"
        with _RECONCILE_LOCK:
            old_seq = int(db.get_int(core.KEY_LAST_SEQUENCE, 0) or 0)
            if seq > old_seq:
                db.set_setting(core.KEY_LAST_SEQUENCE, str(seq))
            db.set_setting(core.KEY_PENDING_VERSION, "")
            db.set_setting(core.KEY_PENDING_SEQUENCE, "")
        logger.info("启动自检：全量安装在途记录 %s 已由当前版本 %s 满足，"
                    "更新序号推进到 %d", pending, current_version,
                    max(old_seq, seq))
        # 全量升级的安装器不会通知用户，托盘是唯一还活着的界面：留一条接力请求
        # 让它冒个气泡，否则用户只能靠「设置页里版本号变了」才知道升级成功。
        core.write_handoff(_handoff_data_dir(), current_version,
                           kind=core.KIND_NOTIFY)
        return "confirmed"
    except Exception as exc:                      # noqa: BLE001 - fail-open
        logger.warning("启动自检确认全量安装失败（不阻断启动，下次继续）：%s", exc)
        return "pending"


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
    # 状态补位（重启后免重新下载）
    # ------------------------------------------------------------------
    def _hydrate_full_retry(self) -> None:
        """进程重启后把「本地已下载、上次没装成」的全量包补成可安装状态。

        现场投诉：全量安装被拦停后手工重启服务也触发不了升级——重启把内存里的
        ``_job`` 清零，界面只剩「重新下载」，用户得再下一遍几百 MB 才能重试。
        安装包其实还在 ``data/update/full-<版本>.exe``（升级不清理 data），这里
        只做状态补位，前端现成的 ``state === 'done'`` 分支就能直接给出
        「安装并重启」，不需要新的接口字段。

        只检查「文件存在且非零」，**不在这里算 sha256**：284MB 同步哈希挂在
        GET /status 上会把界面卡死；完整性由 :meth:`apply` 对着刚验签的
        manifest 复核（size + sha256），损坏照样 fail-closed 停在原版本。
        仅在完全空闲时补位，绝不覆盖进行中的下载/安装状态。
        """
        with self._lock:
            if self._job["state"] != "idle":
                return
            pending = str(self.db.get_setting(core.KEY_PENDING_VERSION, "")
                          or "").strip()
            if not pending or not core.VERSION_RE.match(pending):
                return
            if core.full_install_confirmed(APP_VERSION, pending):
                return                      # 已落地，等启动自检推进即可
            bundle = bundle_root()
            if bundle is None:
                return                      # 开发态 / 旧布局：没有 data/update 可言
            exe = bundle / "data" / "update" / f"full-{pending}.exe"
            try:
                size = exe.stat().st_size
            except OSError:
                return
            if size <= 0:
                return
            self._job = {"state": "done", "done": size, "total": size,
                         "message": "检测到上次下载的全量安装包，可直接重试安装",
                         "version": pending, "action": "optional",
                         "package_type": "full"}
            logger.info("全量安装重试：本地安装包仍在 %s（%d 字节），"
                        "已按「下载完成」状态呈现，可直接重试安装", exe, size)

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
            pending = str(self.db.get_setting(core.KEY_PENDING_VERSION, "")
                          or "").strip()
        action = core.decide(APP_VERSION, m, ignored, last_seen,
                             _channel, ignored_seq)
        # 上次 full 安装只拉起了安装器、新版本还没生效：即使 sequence 被旧版
        # 客户端提前推进过，也必须继续提示，否则用户再也不会收到重试入口。
        pending_unconfirmed = bool(
            pending and not core.full_install_confirmed(APP_VERSION, pending))
        latest = str(m.get("latest", ""))
        if (pending_unconfirmed and action == "none" and latest
                and core.VERSION_RE.match(latest)
                and core.compare_versions(APP_VERSION, latest) < 0):
            try:
                min_sup = str(m.get("min_supported", "0.0.0"))
                action = ("forced" if core.compare_versions(APP_VERSION, min_sup) < 0
                          else "optional")
            except ValueError:
                action = "optional"
            logger.warning(
                "检查更新：全量安装 %s 尚未生效（当前 %s），保留重试提示",
                pending, APP_VERSION)
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
        # 前端据此显示「上次安装未完成，可重试」；正常升级路径恒为 False。
        summary["retry"] = pending_unconfirmed
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
        # 重启后 _job 归零：本地全量包还在就补成「已下载」，让界面直接给出
        # 「安装并重启」，不必再下几百 MB（见 _hydrate_full_retry）。
        # 放在取锁之前——本方法内部要自己拿锁，_lock 不是可重入锁。
        self._hydrate_full_retry()
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
            # full 包是 Inno Setup exe，不是 zip：必须以 .exe 落盘供安装器
            # 直接执行。app 增量包文件名与后续解压/切 junction 链路保持不变。
            is_full = core.is_full_package(pkg)
            package_type = "full" if is_full else "app"
            self._job = {"state": "downloading", "done": 0,
                         "total": int(pkg.get("size", 0) or 0), "message": "",
                         "version": version, "action": "optional",
                         "package_type": package_type}
            dst = bundle / "data" / "update" / (
                f"full-{version}.exe" if is_full else f"app-{version}.zip")
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

        full_install = False
        can_retry_install = False       # 仅「安装器启动失败」时保留已校验的包
        try:
            # 落地前再验一次 manifest，防止检查后被替换（OSS 公共读也要防投毒）。
            fresh, err = self._manifest()
            if err or fresh is None or str(fresh.get("latest", "")) != version:
                raise RuntimeError(f"更新信息已变化，已中止：{err or '版本不一致'}")
            fresh_pkg = core.pick_package(fresh, APP_VERSION)
            if fresh_pkg is None:
                raise RuntimeError("更新信息中无可用安装包，已中止")
            full_install = core.is_full_package(fresh_pkg)
            # 下载阶段判定为 full、二次验签却变成 app（或反之）：类型漂移一律停手，
            # 避免拿错文件去做解压/执行，fail-closed 停在原版本。
            if self._job.get("package_type") == "full" and not full_install:
                raise RuntimeError("更新包类型已变化，已中止")

            if full_install:
                # full 包由 Inno Setup 自己覆盖 bundle 根 + 全部版本层 + runtime，
                # 并保留 data 目录。客户端只负责「复核已下载 exe + detached 拉起」，
                # 绝不切 junction、绝不写 current.json、绝不覆盖 bundle 根文件。
                exe = bundle / "data" / "update" / f"full-{version}.exe"
                if not exe.is_file():
                    raise RuntimeError("全量安装包缺失，已中止")
                size = int(fresh_pkg.get("size", 0) or 0)
                if size > 0 and exe.stat().st_size != size:
                    raise RuntimeError("全量安装包大小不符，已中止")
                sha = str(fresh_pkg.get("sha256", "") or "")
                actual_sha = _sha256_file(exe) or ""
                if sha and actual_sha.lower() != sha.lower():
                    raise RuntimeError("全量安装包 SHA256 校验失败，已中止")
                logger.info("全量安装：安装包复核通过 size=%s sha256=%s exe=%s",
                            size, actual_sha[:12], exe)
                if not core.launch_full_installer(exe):
                    # 包已校验、只是进程没拉起来：保留 done 状态供直接重试安装，
                    # 绝不推进 sequence，也不写 pending（本次根本没有安装）。
                    can_retry_install = True
                    raise RuntimeError("全量安装启动失败，已停在原版本（可重试安装）")
            else:
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

            # 两条分支的确认口径不同：
            # - app 增量包：junction 已切、current.json 已写，本机当场确认成功，
            #   可以推进 sequence；
            # - full 包：只拉起了安装器，装没装上要等新版本启动自检确认。
            #   这里只写 pending，绝不推进 sequence（现场 1.3.2 事故的根因）。
            with self._lock:
                self.db.set_setting(core.KEY_IGNORED_VERSION, "")
                self.db.set_setting(core.KEY_IGNORED_SEQUENCE, "")
                if full_install:
                    self.db.set_setting(core.KEY_PENDING_VERSION, version)
                    self.db.set_setting(core.KEY_PENDING_SEQUENCE,
                                        str(int(fresh.get("sequence", 0) or 0)))
                    logger.info(
                        "全量安装已启动，等待新版本启动自检确认：pending=%s "
                        "sequence=%s；未生效时看安装器日志 %s",
                        version, int(fresh.get("sequence", 0) or 0),
                        core.full_install_log_path(exe))
                    self._job.update({
                        "state": "installing",
                        "message": "全量安装已启动，等待新版本启动确认",
                    })
                else:
                    self.db.set_setting(core.KEY_LAST_SEQUENCE,
                                        str(int(fresh.get("sequence", 0) or 0)))
                    # 版本层已切、current.json 已写 —— 剩下的只是让新代码跑起来。
                    # 请求托盘启动器重启后端：它每 2 秒读一次接力文件，把本进程
                    # 优雅停掉再重新拉起。写请求的一方正是被重启掉的本进程，所以
                    # 后端自己绝不去 spawn 重启（那等于自杀）。
                    if core.write_handoff(_handoff_data_dir(), version):
                        message = "安装完成，正在自动重启后端…"
                    else:
                        message = "安装完成，请从托盘退出后重新打开"
                    self._job.update({"state": "applied", "message": message})
        except Exception as exc:                  # noqa: BLE001
            logger.warning("更新应用失败：%s", exc)
            with self._lock:
                if can_retry_install:
                    self._job.update({
                        "state": "done",
                        "message": "全量安装启动失败；已保留安装包，可直接重试安装",
                    })
                else:
                    self._job.update({"state": "error", "message": str(exc)})
            return 500, {"ok": False, "detail": f"安装失败：{exc}（已停在原版本）"}
        result = {"ok": True, "need_restart": True, "version": version}
        if full_install:
            result["full_install"] = True
            result["install_pending"] = True
        return 200, result

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
