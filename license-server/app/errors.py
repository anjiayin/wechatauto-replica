# -*- coding: utf-8 -*-
"""统一错误处理。

约定（全项目唯一一种错误体，客户端 ``extract_error`` 直接认得）::

    HTTP 4xx/5xx  {"error": {"code": "invalid_code", "message": "中文可操作提示"}}

* ``ApiError``：业务异常，由路由/服务层主动抛出；
* 异常处理器把 FastAPI 默认的 ``{"detail": ...}`` 摊位成顶层 ``error``；
* 参数校验错误（Pydantic）转成 400 ``invalid_param`` + 中文字段名；
* 未预期异常转 500 ``internal_error``，**不泄露堆栈**（只写日志）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

#: 参数校验错误里出现最多的字段 → 中文标签
_FIELD_LABELS: Dict[str, str] = {
    "machine_code": "机器码",
    "activation_code": "激活码",
    "app_version": "客户端版本",
    "os": "操作系统",
    "license_id": "许可证号",
    "username": "用户名",
    "password": "密码",
    "old_password": "原密码",
    "new_password": "新密码",
    "count": "生成数量",
    "days": "天数",
    "customer": "客户名称",
    "customer_name": "客户名称",
    "customer_phone": "手机号",
    "phone": "手机号",
    "edition": "版本",
    "note": "备注",
    "batch": "批次号",
    "expires_at": "到期日",
    "force": "强制标记",
    "reason": "吊销原因",
    "name": "客户名称",
    "q": "搜索关键字",
    "status": "状态",
    "page": "页码",
    "size": "每页条数",
}


class ApiError(Exception):
    """业务异常（code 机器可读，message 中文可操作，status 为 HTTP 状态码）。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code: str = code
        self.message: str = message
        self.status: int = int(status)

    def to_body(self) -> Dict[str, Any]:
        """转成统一错误响应体。"""
        return {"error": {"code": self.code, "message": self.message}}


def err(code: str, message: str, status: int = 400) -> ApiError:
    """``ApiError`` 工厂（便于 ``raise err(...)``）。"""
    return ApiError(code, message, status)


def _validation_message(exc: RequestValidationError) -> str:
    """把 Pydantic 校验错误翻译成一句话中文提示。"""
    errors = exc.errors() or []
    if not errors:
        return "请求参数不正确"
    first = errors[0]
    location = [str(x) for x in (first.get("loc") or []) if str(x) != "body"]
    field = location[-1] if location else "参数"
    label = _FIELD_LABELS.get(field, field)
    kind = str(first.get("type") or "")
    ctx = first.get("ctx") or {}
    if kind == "missing":
        return f"缺少必填参数：{label}"
    if kind in ("string_too_long", "too_long"):
        limit = ctx.get("max_length")
        return f"{label}过长（最多 {limit} 个字符）"
    if kind in ("string_too_short", "too_short"):
        limit = ctx.get("min_length")
        return f"{label}过短（至少 {limit} 个字符）"
    if kind in ("greater_than_equal", "greater_than"):
        return f"{label}超出允许范围（下限 {ctx.get('ge', ctx.get('gt'))}）"
    if kind in ("less_than_equal", "less_than"):
        return f"{label}超出允许范围（上限 {ctx.get('le', ctx.get('lt'))}）"
    if kind in ("int_parsing", "int_type", "float_parsing"):
        return f"{label}必须是整数"
    if kind in ("bool_parsing", "bool_type"):
        return f"{label}必须是布尔值"
    if kind in ("json_invalid", "dict_type", "model_attributes_type"):
        return "请求体不是合法的 JSON 对象"
    detail = str(first.get("msg") or "").strip()
    return f"{label}不合法：{detail}" if detail else f"{label}不合法"


def register_exception_handlers(app: FastAPI) -> None:
    """注册全部异常处理器（在注册路由之前调用即可）。"""

    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        """业务异常 → 统一错误体。"""
        if exc.status >= 500:
            logger.error("业务异常(%s) %s %s", exc.code, request.url.path, exc.message)
        else:
            logger.info("请求被拒绝(%s) %s %s", exc.code, request.url.path, exc.message)
        return JSONResponse(status_code=exc.status, content=exc.to_body())

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_error(request: Request,
                                 exc: StarletteHTTPException) -> JSONResponse:
        """Starlette/FastAPI 抛出的 HTTPException → 摊平成顶层 error。"""
        detail: Any = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            return JSONResponse(status_code=exc.status_code, content=detail)
        if exc.status_code == 404:
            body = {"error": {"code": "not_found",
                              "message": "接口不存在，请检查请求路径"}}
        elif exc.status_code == 405:
            body = {"error": {"code": "method_not_allowed",
                              "message": "请求方法不被支持"}}
        else:
            body = {"error": {"code": "http_error",
                              "message": str(detail) or "请求处理失败"}}
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request,
                                 exc: RequestValidationError) -> JSONResponse:
        """参数校验失败 → 400 invalid_param + 中文提示。"""
        message = _validation_message(exc)
        logger.info("参数校验失败 %s：%s", request.url.path, message)
        return JSONResponse(status_code=400, content={
            "error": {"code": "invalid_param", "message": message}})

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """兜底异常 → 500（日志留完整堆栈，响应体不泄露细节）。"""
        logger.exception("未预期异常 %s %s：%s", request.method,
                         request.url.path, exc)
        return JSONResponse(status_code=500, content={
            "error": {"code": "internal_error",
                      "message": "服务器内部错误，请稍后重试或联系供应商"}})


def error_body(code: str, message: str) -> Dict[str, Any]:
    """构造 200 + ok=false 的业务失败体（心跳授权失效专用）。"""
    return {"ok": False, "error": {"code": code, "message": message}}


def first_error(data: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """从响应体里取 ``{"code","message"}``（测试与非 HTTP 场景用）。"""
    if not isinstance(data, dict):
        return None
    inner = data.get("error")
    return inner if isinstance(inner, dict) else None
