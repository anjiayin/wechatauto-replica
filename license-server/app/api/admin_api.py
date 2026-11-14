# -*- coding: utf-8 -*-
"""管理端业务接口（B1–B17）。

路由级依赖：``require_admin``（会话）+ ``require_csrf``（写操作双提交校验）+
``require_password_changed``（强制改密拦截）。
所有时间字段统一输出带 ``+08:00`` 的 ISO 字符串，前端无需猜时区。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from app import db, signing
from app.api.deps import (get_client_ip, require_admin, require_csrf,
                          require_password_changed)
from app.errors import ApiError
from app.models import (BindCustomerIn, CreateCodesIn, CreateCustomerIn,
                        ExtendIn, RenewIn, RevokeIn, SetExpiryIn)
from app.repo import audit as audit_repo
from app.repo import codes as codes_repo
from app.repo import customers as customers_repo
from app.repo import licenses as licenses_repo
from app.services import admin_service

logger = logging.getLogger(__name__)

_GUARDS = [Depends(require_admin), Depends(require_csrf),
           Depends(require_password_changed)]

router = APIRouter(prefix="/admin/api", tags=["管理端业务"],
                   dependencies=_GUARDS)


# ---------------------------------------------------------------------------
# 序列化工具
# ---------------------------------------------------------------------------
def _iso(value: Any) -> Any:
    """datetime → 带 +08:00 的 ISO 字符串（其它类型原样返回）。"""
    if isinstance(value, datetime):
        return signing.to_iso(value)
    return value


def _row(row: Dict[str, Any]) -> Dict[str, Any]:
    """把一行的 datetime 字段统一转成 ISO 字符串。"""
    return {key: _iso(value) for key, value in row.items()}


def _rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """批量转换。"""
    return [_row(row) for row in rows]


def _paging(page: int, size: int) -> tuple[int, int]:
    """分页参数夹取（防负数/超大页）。"""
    return max(1, int(page)), max(1, min(int(size), 200))


def _annotate(rows: List[Dict[str, Any]],
              moment: datetime) -> List[Dict[str, Any]]:
    """给列表行补 ``role`` / ``can_restore``。

    ``rows`` 是按 ``id DESC``（新在前）排列的原始行：
    每台机器**最新的一条 current_key 非空**记录才是 active/expired，
    其余同机的 current_key 非空记录属于异常态，标记为 superseded。
    """
    owner: Dict[str, int] = {}
    for row in rows:
        machine = str(row.get("machine_code") or "")
        if row.get("current_key") is not None and machine not in owner:
            owner[machine] = int(row.get("id") or 0)

    result: List[Dict[str, Any]] = []
    for row in rows:
        machine = str(row.get("machine_code") or "")
        item = dict(row)
        if row.get("revoked_at"):
            item["role"] = "revoked"
            item["can_restore"] = machine not in owner
        elif row.get("current_key") is None:
            item["role"] = "superseded"
            item["can_restore"] = False
        elif owner.get(machine) == int(row.get("id") or 0):
            expires = row.get("expires_at")
            item["role"] = ("expired"
                            if expires is not None and expires <= moment
                            else "active")
            item["can_restore"] = False
        else:
            item["role"] = "superseded"
            item["can_restore"] = False
        result.append(item)
    return result


def _actor(request: Request) -> Dict[str, Any]:
    """当前操作用户（审计用）。"""
    return request.state.user


# ---------------------------------------------------------------------------
# B1 仪表盘 / B17 字典
# ---------------------------------------------------------------------------
@router.get("/dashboard", summary="B1 仪表盘统计")
def dashboard(days: int = Query(30, ge=7, le=90)) -> Dict[str, Any]:
    """全部统计口径（激活码/许可证/机器/聚合/趋势/最近激活）。"""
    data = admin_service.dashboard(days)
    data["trend"] = _rows(data.get("trend") or [])
    data["by_phone"] = _rows(data.get("by_phone") or [])
    data["by_customer"] = _rows(data.get("by_customer") or [])
    data["recent_activations"] = _rows(data.get("recent_activations") or [])
    data["generated_at"] = signing.to_iso(data.get("generated_at"))
    return data


@router.get("/meta", summary="B17 字典与默认值")
def meta() -> Dict[str, Any]:
    """前端渲染用的字典（版本选项、默认天数、限流参数）。"""
    data = admin_service.meta()
    data["server_time"] = signing.to_iso(data.get("server_time"))
    return data


# ---------------------------------------------------------------------------
# B2–B4 激活码
# ---------------------------------------------------------------------------
@router.post("/codes", summary="B2 批量生成激活码")
def create_codes(body: CreateCodesIn, request: Request) -> Dict[str, Any]:
    """生成激活码（``customer`` 兼容 mock 的入参名）。"""
    result = admin_service.create_codes(
        count=body.count, days=body.days, customer=body.customer,
        customer_phone=body.customer_phone, edition=body.edition,
        note=body.note, batch=body.batch, actor=_actor(request),
        ip=get_client_ip(request))
    result["items"] = _rows(result.get("items") or [])
    return result


@router.get("/codes", summary="B3 激活码列表")
def list_codes(q: str = Query("", max_length=64),
               status: str = Query("all", max_length=16),
               phone: str = Query("", max_length=32),
               batch: str = Query("", max_length=32),
               page: int = Query(1, ge=1),
               size: int = Query(20, ge=1, le=200)) -> Dict[str, Any]:
    """激活码分页列表（支持状态/批次/手机号/关键字筛选）。"""
    page_no, page_size = _paging(page, size)
    with db.transaction() as cur:
        items, total = codes_repo.list_page(
            cur, keyword=q, status=status, phone=phone, batch=batch,
            page=page_no, size=page_size)
    return {"ok": True, "items": _rows(items), "total": total,
            "page": page_no, "size": page_size}


@router.get("/codes/export", summary="B4 导出激活码（CSV，含筛选与完整字段）",
            response_class=PlainTextResponse)
def export_codes(q: str = Query("", max_length=64),
                 status: str = Query("all", max_length=16),
                 phone: str = Query("", max_length=32),
                 batch: str = Query("", max_length=32)) -> PlainTextResponse:
    """导出激活码（CSV，Excel 可直接打开）。

    **与列表共用同一套筛选参数**（关键字/状态/手机号/批次），导出的就是当前
    筛选结果。列：激活码、天数、版本、批次、客户、手机号、状态、绑定机器码、
    使用时间、生成时间、生成人、备注；带 BOM 便于 Excel 识别 UTF-8。
    """
    with db.transaction() as cur:
        rows = codes_repo.export_rows(cur, keyword=q, status=status,
                                      phone=phone, batch=batch)

    def cell(value: Any) -> str:
        text = "" if value is None else str(value)
        if any(ch in text for ch in (",", '"', "\n", "\r")):
            return '"' + text.replace('"', '""') + '"'
        return text

    def status_text(row: Dict[str, Any]) -> str:
        if row.get("voided_at") is not None:
            return "已作废"
        return "已使用" if str(row.get("used_machine") or "") else "未使用"

    header = ["激活码", "天数", "版本", "批次", "客户", "手机号", "状态",
              "绑定机器码", "使用时间", "生成时间", "生成人", "备注"]
    lines = [",".join(header)]
    for row in rows:
        lines.append(",".join(cell(x) for x in (
            row.get("code"), row.get("days"), row.get("edition"),
            row.get("batch"), row.get("customer_name"),
            row.get("customer_phone"), status_text(row),
            row.get("used_machine") or "",
            signing.to_iso(row["used_at"]) if row.get("used_at") else "",
            signing.to_iso(row["created_at"]) if row.get("created_at") else "",
            row.get("created_by"), row.get("note"))))
    body = "\ufeff" + "\r\n".join(lines) + ("\r\n" if rows else "")
    logger.info("导出激活码 %d 条（筛选 q=%r status=%r phone=%r batch=%r）",
                len(rows), q, status, phone, batch)
    return PlainTextResponse(
        content=body, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 'attachment; filename="activation-codes.csv"'})


@router.post("/codes/{code}/void", summary="B19 作废未使用的激活码")
def void_code(code: str, request: Request) -> Dict[str, Any]:
    """销毁一个未使用的激活码（已使用的码不能作废，只能吊销许可证）。"""
    return admin_service.void_code(code=code, actor=_actor(request),
                                   ip=get_client_ip(request))


# ---------------------------------------------------------------------------
# B5–B11 许可证
# ---------------------------------------------------------------------------
@router.get("/licenses", summary="B5 许可证列表")
def list_licenses(q: str = Query("", max_length=64),
                  status: str = Query("all", max_length=16),
                  phone: str = Query("", max_length=32),
                  page: int = Query(1, ge=1),
                  size: int = Query(20, ge=1, le=200)) -> Dict[str, Any]:
    """许可证分页列表（每行带 ``role``：当前生效/已过期/已吊销/已被取代）。"""
    page_no, page_size = _paging(page, size)
    moment = signing.now()
    with db.transaction() as cur:
        rows, total = licenses_repo.list_page(
            cur, keyword=q, status=status, phone=phone, page=page_no,
            size=page_size, now=moment)
    annotated = _annotate(rows, moment)
    return {"ok": True, "items": _rows(annotated), "total": total,
            "page": page_no, "size": page_size}


@router.get("/licenses/{license_id}", summary="B11 许可证详情")
def license_detail(license_id: str) -> Dict[str, Any]:
    """单证详情 + 该机器全部历史 + 该机器用过的激活码。"""
    moment = signing.now()
    with db.transaction() as cur:
        record = licenses_repo.find_by_license_id(cur, license_id)
        if record is None:
            raise ApiError("not_found", f"许可证不存在：{license_id}", 404)
        history = licenses_repo.history_by_machine(
            cur, str(record["machine_code"]))
        cur.execute(
            "SELECT id, code, days, customer_name, customer_phone, batch, "
            "       used_machine, used_at, created_at FROM license_code "
            "WHERE used_machine = %s ORDER BY id DESC LIMIT 50",
            (str(record["machine_code"]),))
        used_codes = list(cur.fetchall() or [])
        audited = audit_repo.list_page(cur, action="", keyword="",
                                       machine_code=str(record["machine_code"]),
                                       page=1, size=30)
    annotated = _annotate(history, moment)
    return {"ok": True, "item": _row(annotated[0] if annotated else record),
            "history": _rows(annotated),
            "codes": _rows(used_codes),
            "audit": {"items": _rows(audited[0]), "total": audited[1]}}


@router.post("/licenses/{license_id}/renew", summary="B6 延期（mock 兼容别名）")
def renew(license_id: str, body: RenewIn, request: Request) -> Dict[str, Any]:
    """延期（只接受正数，规则同 ``extend`` 的正数分支）。"""
    return admin_service.extend(license_id=license_id, days=int(body.days),
                                force=False, actor=_actor(request),
                                ip=get_client_ip(request))


@router.post("/licenses/{license_id}/extend", summary="B7 增减天数")
def extend(license_id: str, body: ExtendIn, request: Request) -> Dict[str, Any]:
    """统一增减天数（``days<0`` 即缩期）。"""
    return admin_service.extend(license_id=license_id, days=int(body.days),
                                force=bool(body.force),
                                actor=_actor(request),
                                ip=get_client_ip(request))


@router.post("/licenses/{license_id}/expiry", summary="B8 直接设到期日")
def set_expiry(license_id: str, body: SetExpiryIn,
               request: Request) -> Dict[str, Any]:
    """直接设定到期日（``YYYY-MM-DD`` 解释为当天 23:59:59）。"""
    return admin_service.set_expiry(license_id=license_id,
                                    expires_at=body.expires_at,
                                    force=bool(body.force),
                                    actor=_actor(request),
                                    ip=get_client_ip(request))


@router.post("/licenses/{license_id}/revoke", summary="B9 吊销")
def revoke(license_id: str, request: Request,
           body: Optional[RevokeIn] = None) -> Dict[str, Any]:
    """吊销（客户下次心跳收到 ``revoked`` 并锁死）。"""
    payload = body or RevokeIn()
    return admin_service.revoke(license_id=license_id, reason=payload.reason,
                               force=bool(payload.force),
                               actor=_actor(request),
                               ip=get_client_ip(request))


@router.post("/licenses/{license_id}/restore", summary="B10 恢复")
def restore(license_id: str, request: Request) -> Dict[str, Any]:
    """撤销吊销（客户下次心跳即恢复）。"""
    return admin_service.restore(license_id=license_id, actor=_actor(request),
                                 ip=get_client_ip(request))


@router.post("/licenses/{license_id}/customer", summary="B18 补录/修改客户绑定")
def bind_customer(license_id: str, body: BindCustomerIn,
                  request: Request) -> Dict[str, Any]:
    """给已有许可证补录客户名称与手机号（名称/手机号至少填一个）。"""
    return admin_service.bind_customer(license_id=license_id,
                                       customer=body.customer,
                                       phone=body.phone,
                                       actor=_actor(request),
                                       ip=get_client_ip(request))


# ---------------------------------------------------------------------------
# B12–B15 客户与机器
# ---------------------------------------------------------------------------
@router.get("/customers", summary="B12 客户列表")
def list_customers(q: str = Query("", max_length=64),
                   page: int = Query(1, ge=1),
                   size: int = Query(20, ge=1, le=200)) -> Dict[str, Any]:
    """客户档案列表（含机器数/生效数/最近到期/最近心跳）。"""
    page_no, page_size = _paging(page, size)
    with db.transaction() as cur:
        items, total = customers_repo.list_with_stats(
            cur, q, page_no, page_size, signing.now())
    return {"ok": True, "items": _rows(items), "total": total,
            "page": page_no, "size": page_size}


@router.post("/customers", summary="B13 新增/更新客户档案")
def create_customer(body: CreateCustomerIn, request: Request) -> Dict[str, Any]:
    """按手机号 upsert 客户档案。"""
    return admin_service.upsert_customer(
        name=body.name, phone=body.phone, note=body.note,
        actor=_actor(request), ip=get_client_ip(request))


@router.get("/customers/{customer_id}", summary="B14 客户详情")
def customer_detail(customer_id: int) -> Dict[str, Any]:
    """客户详情 + 该客户所有机器与激活码。"""
    moment = signing.now()
    with db.transaction() as cur:
        record = customers_repo.get_by_id(cur, customer_id)
        if record is None:
            raise ApiError("not_found", f"客户不存在：{customer_id}", 404)
        phones = [str(record.get("phone") or "")]
        cur.execute(
            "SELECT id, license_id, machine_code, customer_name, "
            "       customer_phone, edition, expires_at, revoked_at, "
            "       last_seen_at, current_key, created_at "
            "FROM license WHERE customer_id = %s OR customer_phone = %s "
            "ORDER BY id DESC LIMIT 200",
            (int(customer_id), phones[0]))
        machines = _annotate(list(cur.fetchall() or []), moment)
        cur.execute(
            "SELECT id, code, days, edition, batch, note, used_machine, "
            "       used_at, created_at FROM license_code "
            "WHERE customer_id = %s OR customer_phone = %s "
            "ORDER BY id DESC LIMIT 200",
            (int(customer_id), phones[0]))
        code_rows = list(cur.fetchall() or [])
    return {"ok": True, "item": _row(record), "machines": _rows(machines),
            "codes": _rows(code_rows)}


@router.get("/machines", summary="B15 按手机号/客户名定位机器码")
def list_machines(q: str = Query("", max_length=64)) -> Dict[str, Any]:
    """轻量机器清单（只回当前生效记录，按到期日升序）。"""
    text = str(q or "").strip()
    like = f"%{text}%"
    moment = signing.now()
    with db.transaction() as cur:
        cur.execute(
            "SELECT id, license_id, machine_code, customer_name, "
            "       customer_phone, expires_at, last_seen_at "
            "FROM license "
            "WHERE current_key IS NOT NULL AND "
            "      (%s = '' OR machine_code LIKE %s OR customer_name LIKE %s "
            "       OR customer_phone LIKE %s) "
            "ORDER BY expires_at ASC LIMIT 50",
            (text, like, like, like))
        rows = list(cur.fetchall() or [])
    items: List[Dict[str, Any]] = []
    for row in rows:
        expires = row.get("expires_at")
        items.append({
            "id": int(row.get("id") or 0),
            "license_id": str(row.get("license_id") or ""),
            "machine_code": str(row.get("machine_code") or ""),
            "customer_name": str(row.get("customer_name") or ""),
            "customer_phone": str(row.get("customer_phone") or ""),
            "expires_at": _iso(expires),
            "last_seen_at": _iso(row.get("last_seen_at")),
            "status": "expired" if (expires is not None and expires <= moment)
                      else "active",
        })
    return {"ok": True, "items": items}


# ---------------------------------------------------------------------------
# B16 审计
# ---------------------------------------------------------------------------
@router.get("/audit", summary="B16 审计日志")
def list_audit(action: str = Query("", max_length=32),
               machine_code: str = Query("", max_length=40),
               q: str = Query("", max_length=64),
               page: int = Query(1, ge=1),
               size: int = Query(20, ge=1, le=200)) -> Dict[str, Any]:
    """审计日志（倒序，可按动作/机器码/关键字筛选）。"""
    page_no, page_size = _paging(page, size)
    with db.transaction() as cur:
        items, total = audit_repo.list_page(
            cur, action=action, machine_code=machine_code, keyword=q,
            page=page_no, size=page_size)
        actions = audit_repo.actions(cur)
    return {"ok": True, "items": _rows(items), "total": total,
            "page": page_no, "size": page_size, "actions": actions}
