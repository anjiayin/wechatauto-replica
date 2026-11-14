# -*- coding: utf-8 -*-
"""客户端接口（无需鉴权，路径写死在客户端里）。

| # | 方法 | 路径 | 说明 |
|---|---|---|---|
| C1 | POST | /api/v1/license/activate | 激活（限流：IP + 机器码） |
| C2 | POST | /api/v1/license/heartbeat | 心跳（授权失效用 200 + ok=false） |
| C3 | GET | /api/v1/license/query | 查询（限流：IP） |
| C4 | GET | /api/health | 健康检查（DB 探活） |
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app import db, ratelimit, signing
from app.api.deps import get_client_ip
from app.config import get_config
from app.errors import ApiError
from app.models import ActivateIn, HeartbeatIn
from app.services import license_service

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/v1/license/activate", summary="C1 激活")
def activate(body: ActivateIn, request: Request) -> dict:
    """用激活码换许可证（限流键：客户端 IP + 机器码）。"""
    ip = get_client_ip(request)
    machine = signing.normalize_machine(body.machine_code)
    if not ratelimit.activate_limiter().allow((ip, machine)):
        logger.warning("激活限流命中：ip=%s machine=%s", ip, machine)
        raise ApiError("rate_limited", "请求过于频繁，请 1 分钟后再试", 429)
    return license_service.activate(
        machine_code=machine,
        activation_code=body.activation_code,
        app_version=body.app_version,
        os_name=body.os,
        ip=ip,
    )


@router.post("/api/v1/license/heartbeat", summary="C2 心跳")
def heartbeat(body: HeartbeatIn, request: Request) -> dict:
    """联网确认（客户端每 6 小时一次；成功后重新下发许可证）。"""
    return license_service.heartbeat(
        machine_code=body.machine_code,
        license_id=body.license_id,
        app_version=body.app_version,
        ip=get_client_ip(request),
    )


@router.get("/api/v1/license/query", summary="C3 查询授权")
def query(request: Request,
          machine_code: str = Query("", max_length=64)) -> dict:
    """查询某机器码的授权（客服核对用）。"""
    ip = get_client_ip(request)
    if not ratelimit.query_limiter().allow(ip):
        raise ApiError("rate_limited", "请求过于频繁，请 1 分钟后再试", 429)
    return license_service.query(machine_code=machine_code)


@router.get("/api/health", summary="C4 健康检查")
def health() -> JSONResponse:
    """DB 探活；不通时返回 503（便于反代/监控挂载）。"""
    cfg = get_config()
    ok = db.ping()
    body = {
        "ok": ok,
        "db": ok,
        "version": "1.0.0",
        "key_id": cfg.key_id,
        "time": signing.to_iso(signing.now()),
    }
    if not ok:
        body["error"] = {"code": "db_unavailable",
                         "message": "数据库连接不可用，请检查 LS_DB_* 配置与网络"}
        return JSONResponse(status_code=503, content=body)
    return JSONResponse(status_code=200, content=body)
