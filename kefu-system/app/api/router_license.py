# -*- coding: utf-8 -*-
"""电脑授权（license）路由。

路由（供前端激活界面使用，契约见 docs/授权服务接口文档.md）：
- GET  /api/license/status               当前授权状态（前端 5 秒轮询 / 首屏）
- GET  /api/license/machine-code         本机机器码（含成分，deep=1 时附 WMI 明细）
- POST /api/license/activate             激活码激活（联网请求授权服务器签发）
- POST /api/license/refresh              立即联网确认一次（改了到期日/续费后点）

**这些接口一律不受授权拦截中间件限制**——否则未授权时连激活都做不到。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from app import license as license_mod
from app.api import deps
from app.schemas import LicenseActivateIn, LicenseStatusOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/license", tags=["license"])


def _client() -> license_mod.LicenseClient:
    """授权客户端（main.py 注入带 db 的实例；未注入时退回默认实例）。"""
    return deps.get_license_client()


@router.get("/status", response_model=LicenseStatusOut)
def get_status(
    touch: bool = Query(False, description="是否顺便刷新 max_seen_ts"),
) -> LicenseStatusOut:
    """当前授权状态。"""
    return LicenseStatusOut(**_status_dict(_client().status(touch=touch)))


@router.get("/machine-code")
def get_machine_code(deep: bool = Query(
        False, description="是否附带 WMI 硬件明细（WMI 坏时可能等 5~15 秒）")) -> dict:
    """本机机器码（客户把这串报给厂商申请授权）。"""
    info = license_mod.machine_info(deep=deep, code=_client().machine_code())
    return {"ok": True, **info}


@router.post("/activate", response_model=LicenseStatusOut)
def activate(body: LicenseActivateIn) -> LicenseStatusOut:
    """用激活码激活本机。

    成功后若原本因未授权没启动抓取，这里会自动把监听/分析拉起来，
    用户不必手动重启程序。
    """
    client = _client()
    try:
        status = client.activate(body.activation_code)
    except license_mod.LicenseError as exc:
        logger.warning("激活失败（%s）：%s", exc.code, exc)
        raise HTTPException(status_code=400,
                            detail=f"激活失败：{exc}（{_hint(exc.code)}）") from exc
    if status.ok:
        deps.ensure_runtime_started()
    logger.info("激活成功：%s（到期 %s）", status.license_id, status.expires_at)
    return LicenseStatusOut(**_status_dict(status))


@router.post("/refresh", response_model=LicenseStatusOut)
def refresh() -> LicenseStatusOut:
    """立即联网确认一次（续费后不必等后台定时刷新）。"""
    client = _client()
    status = client.refresh()
    if status.ok:
        deps.ensure_runtime_started()
    return LicenseStatusOut(**_status_dict(status))


def _status_dict(status: license_mod.LicenseStatus) -> dict:
    """LicenseStatus → dict（response_model 会自动裁剪字段）。"""
    return status.to_dict()


_ERROR_HINTS = {
    "network": "请检查网络或授权服务器地址（设置页 license.server_url）",
    "invalid_code": "激活码不正确或已被使用，请核对后重试",
    "code_used": "该激活码已被其它电脑使用，请联系供应商",
    "machine_mismatch": "许可证与本机机器码不匹配",
    "not_found": "服务器上没有这台电脑的授权记录",
    "revoked": "该机器码的授权已被吊销；旧激活码不能重复使用，"
               "请让供应商重新发一个激活码",
    "bad_license": "服务器返回的许可证无法验签，请联系供应商",
    "expired": "授权已到期，请续费",
}


def _hint(code: str) -> str:
    """把服务端错误码翻译成可操作的中文建议。"""
    return _ERROR_HINTS.get(code, "请稍后重试或联系供应商")
