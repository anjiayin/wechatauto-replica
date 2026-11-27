# -*- coding: utf-8 -*-
"""FastAPI 依赖注入：持有应用级单例（由 main.py lifespan 组装注入）。"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable, Optional

from fastapi import HTTPException

from app import license as license_mod
from app.db import Database
from app.notifier import Notifier
from app.pipeline import Pipeline
from app.sender import Sender
from app.summary import SummaryService
from app.wechat_service import WeChatService

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_db: Database | None = None
_wechat: WeChatService | None = None
_pipeline: Pipeline | None = None
_sender: Sender | None = None
_notifier: Notifier | None = None
_license: Optional[license_mod.LicenseClient] = None
_runtime_start: Optional[Callable[[], None]] = None
_runtime_started = False


def configure(db: Database, wechat: WeChatService, pipeline: Pipeline,
              sender: Sender, notifier: Notifier,
              license_client: Optional[
                  license_mod.LicenseClient] = None) -> None:
    """应用启动时注入全部单例（main.py 调用）。

    Args:
        license_client: 授权客户端。授权未启用/未注入时 get_license_client()
            会退回进程级默认实例（等价于「未启用授权」），不影响自用与测试。
    """
    global _db, _wechat, _pipeline, _sender, _notifier, _license
    with _lock:
        _db = db
        _wechat = wechat
        _pipeline = pipeline
        _sender = sender
        _notifier = notifier
        if license_client is not None:
            _license = license_client


def configure_runtime(start: Callable[[], None]) -> None:
    """注入「启动抓取/分析/发送」的回调（授权通过后由 main.py 调用）。

    未授权时这些线程不启动（省资源、避免白跑），激活成功后通过这个回调
    现场拉起，用户不需要重启程序。
    """
    global _runtime_start
    with _lock:
        _runtime_start = start


def ensure_runtime_started() -> bool:
    """确保运行时已启动；本次真正启动了返回 True（重复调用是幂等的）。"""
    global _runtime_started
    with _lock:
        if _runtime_started or _runtime_start is None:
            return False
        _runtime_started = True
        starter = _runtime_start
    try:
        starter()
    except Exception:
        with _lock:               # 启动失败允许下次重试
            _runtime_started = False
        logger.exception("授权通过后启动抓取/分析失败")
        return False
    logger.info("授权通过，已启动微信监听 / AI 分析 / 发送线程")
    return True


def mark_runtime_started() -> None:
    """标记运行时已启动（main.py 在授权正常、直接启动时调用）。"""
    global _runtime_started
    with _lock:
        _runtime_started = True


def reset_for_tests() -> None:
    """清空全部单例（仅测试用，避免用例之间串味）。"""
    global _db, _wechat, _pipeline, _sender, _notifier, _license
    global _runtime_start, _runtime_started
    with _lock:
        _db = _wechat = _pipeline = _sender = _notifier = None
        _license = None
        _runtime_start = None
        _runtime_started = False


def _require(instance, name: str):
    """单例缺失时给出中文 500（正常不会发生，防御性兜底）。"""
    if instance is None:
        raise HTTPException(status_code=500,
                            detail=f"系统尚未完成初始化（{name}）")
    return instance


def get_db() -> Database:
    """业务数据库单例。"""
    return _require(_db, "数据库")


def get_wechat_service() -> WeChatService:
    """微信接入服务单例。"""
    return _require(_wechat, "微信服务")


def get_wechat_service_optional() -> WeChatService | None:
    """微信服务单例（未注入时返回 None，不抛异常）。

    给「有更好、没有也能用」的场景（如商机列表的企业名补齐）：列表接口
    的核心数据只来自业务库，补显示名是附加增强，绝不能因为微信服务没
    起来就让整页 500。
    """
    return _wechat


def get_pipeline() -> Pipeline:
    """分析管线单例。"""
    return _require(_pipeline, "分析管线")


def get_sender() -> Sender:
    """发送服务单例。"""
    return _require(_sender, "发送服务")


def get_notifier() -> Notifier:
    """WS 通知中心单例。"""
    return _require(_notifier, "通知中心")


def get_summary_service() -> SummaryService:
    """群消息总结服务（请求级轻量构造，无常驻单例）。"""
    return SummaryService(get_db(), get_wechat_service())


def get_license_client() -> license_mod.LicenseClient:
    """授权客户端。

    未注入时退回进程级默认实例（没有 db → 读环境变量/默认值，等价于
    「未启用授权」），保证任何调用点都不会因为没初始化而 500。
    """
    with _lock:
        if _license is not None:
            return _license
    return license_mod.get_client()


def current_account(svc: Any) -> str:
    """当前生效的监控账号目录名（多开隔离的过滤键）。

    微信服务未注入/测试替身没有 ``effective_account`` 时返回空串，
    调用方按「不过滤」处理，保持单账号与测试场景的旧行为。
    """
    getter = getattr(svc, "effective_account", None)
    if not callable(getter):
        return ""
    try:
        return str(getter() or "").strip()
    except Exception:
        logger.debug("读取当前监控账号失败（按全部账号处理）", exc_info=True)
        return ""


def account_display_name(svc: Any, account: str) -> str:
    """账号目录名 → 微信昵称（读不到时退回目录名去掉随机后缀）。

    wxid 类目录形如 ``wxid_xxx_ab12``，末尾 4 位是客户端生成的后缀，
    兜底展示时剥掉更像昵称；真实昵称由微信服务提供。
    """
    value = str(account or "").strip()
    if not value:
        return ""
    getter = getattr(svc, "account_display", None)
    if callable(getter):
        try:
            name = str(getter(value) or "").strip()
        except Exception:
            logger.debug("读取账号显示名失败：%s", value, exc_info=True)
            name = ""
        if name:
            return name
    return re.sub(r"_[0-9a-z]{4}$", "", value, flags=re.IGNORECASE)
