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

import json
import logging
import os
import shutil
import threading
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
    开发态（APP_ROOT = 仓库/kefu-system）返回 None，接口据此报告「未安装为
    客户端版本」，避免误操作源码目录。
    """
    explicit = os.environ.get("KEFU_BUNDLE_ROOT", "").strip()
    if explicit:
        return Path(explicit)
    root = Path(APP_ROOT).resolve()
    if root.name == "kefu-system" and root.parent.name == "current" \
            and root.parent.parent.name == "app":
        return root.parent.parent.parent
    return None


class UpdateService:
    """更新流程状态机（进程级单例，由 router_update 持有）。"""

    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()
        self._last_manifest: dict[str, Any] | None = None
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
            last_seen = self.db.get_int(core.KEY_LAST_SEQUENCE, 0)
        action = core.decide(APP_VERSION, m, ignored, last_seen, _channel)
        summary = core.public_summary(m, action, APP_VERSION)
        pkg = core.pick_package(m, APP_VERSION)
        if pkg and action != "none":
            summary["package_size"] = int(pkg.get("size", 0) or 0)
            summary["package_type"] = str(pkg.get("type", ""))
        summary["ok"] = True
        with self._lock:
            self._last_manifest = m if action != "none" else None
        logger.info("检查更新：current=%s latest=%s action=%s",
                    APP_VERSION, summary.get("latest"), action)
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
                self._job.update({"state": "applied", "message": "安装完成，请重启"})
        except Exception as exc:                  # noqa: BLE001
            logger.warning("更新应用失败：%s", exc)
            with self._lock:
                self._job.update({"state": "error", "message": str(exc)})
            return 500, {"ok": False, "detail": f"安装失败：{exc}（已停在原版本）"}
        return 200, {"ok": True, "need_restart": True, "version": version}

    def ignore(self, version: str) -> dict[str, Any]:
        with self._lock:
            self.db.set_setting(core.KEY_IGNORED_VERSION, str(version or ""))
            self._last_manifest = None
            self._job = {"state": "idle", "done": 0, "total": 0,
                         "message": "", "version": "", "action": "none"}
        return {"ok": True, "ignored": str(version or "")}
