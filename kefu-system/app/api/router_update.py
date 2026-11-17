# -*- coding: utf-8 -*-
"""客户端自动更新路由（前端更新 UI 的后端契约）。

- GET  /api/system/update/check      检查版本（拉 OSS manifest + Ed25519 验签 + 三级判定）
- POST /api/system/update/download   确认关杀软后开始下载（202 + 后台进度）
- GET  /api/system/update/status     查询下载/安装进度
- POST /api/system/update/apply      校验通过后切换版本（成功后需重启生效）
- POST /api/system/update/ignore     忽略某个版本

**这些接口不受授权拦截**：授权过期时客户更要能升级修复，且检查/下载不触碰
业务数据。真正的写操作（切 junction）只在验签 + 完整性自检通过后执行。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api import deps
from app.update_service import UpdateService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/system/update", tags=["system-update"])

_service: UpdateService | None = None


class DownloadIn(BaseModel):
    confirmed: bool = False


class IgnoreIn(BaseModel):
    version: str = ""


def get_service() -> UpdateService:
    """进程级单例（db 由 main.py lifespan 注入；测试可 set_service 替换）。"""
    global _service
    if _service is None:
        _service = UpdateService(deps.get_db())
    return _service


def set_service(service: UpdateService | None) -> None:
    global _service
    _service = service


@router.get("/check")
def check() -> dict[str, Any]:
    return get_service().check()


@router.post("/download")
def download(body: DownloadIn):
    code, payload = get_service().download(bool(body.confirmed))
    return JSONResponse(status_code=code, content=payload)


@router.get("/status")
def status() -> dict[str, Any]:
    return get_service().status()


@router.post("/apply")
def apply():
    code, payload = get_service().apply()
    return JSONResponse(status_code=code, content=payload)


@router.post("/ignore")
def ignore(body: IgnoreIn) -> dict[str, Any]:
    return get_service().ignore(body.version)
