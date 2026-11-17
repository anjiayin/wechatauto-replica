# -*- coding: utf-8 -*-
"""授权拦截中间件：授权不可用时拦住全部业务接口，只留激活/状态查询。

**只锁功能，不动数据**：数据库、图片缓存、语音缓存、日志一概不删不改，
续费/重新激活后原样恢复。这一点很重要——授权过期是商务问题，不该变成
数据事故。

白名单（未授权时仍放行）：
- 非 ``/api/`` 路径：静态资源、SPA 页面、``/ws/events``（前端要显示状态）
- ``/api/license/*``：否则未授权时连激活接口都调不了，形成死锁
- ``/api/status``、``/api/health``：前端首屏需要它们渲染授权横幅
- ``/api/system/update/*``：授权过期时更要能升级修复；检查/下载不碰业务数据

其余 ``/api/*`` 一律 403 + 中文原因 + ``license_state``，前端据此弹激活框。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# 未授权时仍放行的精确路径（前缀匹配用 _ALLOW_PREFIXES）
ALLOW_EXACT = {
    "/api/status",
    "/api/health",
    "/api/license/status",
    "/api/license/machine-code",
    "/api/license/activate",
    "/api/license/refresh",
}
ALLOW_PREFIXES = ("/api/license/", "/api/system/update/")

# 状态缓存（秒）：每个业务请求都验签+读盘没必要，3 秒足够灵敏
_STATUS_CACHE_SEC = 3.0


def _allowed(path: str) -> bool:
    """该路径是否免于授权拦截。"""
    if path in ALLOW_EXACT:
        return True
    return any(path.startswith(p) for p in ALLOW_PREFIXES)


def make_license_guard(
    status_provider: Callable[[], Any],
    cache_sec: float = _STATUS_CACHE_SEC,
) -> Callable:
    """构造中间件函数（由 main.py 用 ``app.middleware("http")`` 挂载）。

    Args:
        status_provider: 返回 :class:`app.license.LicenseStatus` 的可调用对象。
        cache_sec: 状态缓存秒数；0 表示不缓存（测试用）。
    """
    cache: dict[str, Any] = {"at": 0.0, "status": None}

    def _status():
        """读授权状态（带短缓存，避免每个请求都验签）。"""
        if cache_sec > 0:
            age = time.monotonic() - cache["at"]
            if cache["status"] is not None and age < cache_sec:
                return cache["status"]
        st = status_provider()
        cache["at"] = time.monotonic()
        cache["status"] = st
        return st

    async def guard(request: Request, call_next) -> Any:
        """按授权状态决定放行或 403。"""
        path = request.url.path
        if not path.startswith("/api/") or _allowed(path):
            return await call_next(request)
        try:
            status = _status()
        except Exception:            # 授权模块自身异常绝不阻塞业务
            logger.exception("读取授权状态失败（按放行处理）")
            return await call_next(request)
        if getattr(status, "ok", True):
            return await call_next(request)
        state = str(getattr(status, "state", "") or "")
        message = str(getattr(status, "message", "") or "授权不可用")
        logger.info("拦截请求 %s %s（授权状态 %s）",
                    request.method, path, state)
        return JSONResponse(
            status_code=403,
            content={
                "detail": f"{message}（当前不可用：业务功能已暂停，数据未受影响）",
                "code": "license_required",
                "license_state": state,
                "license_message": message,
                "machine_code": getattr(status, "machine_code", ""),
            },
        )

    return guard


def cached_status_provider(
    client_getter: Optional[Callable] = None,
) -> Callable:
    """返回一个读取授权状态的 provider（默认走 deps 注入的客户端）。"""
    def provider():
        if client_getter is not None:
            client = client_getter()
        else:
            from app.api import deps      # 延迟导入，避免循环依赖
            client = deps.get_license_client()
        return client.status()

    return provider
