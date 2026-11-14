# -*- coding: utf-8 -*-
"""管理端业务规则：延期 / 缩期 / 设到期日 / 吊销 / 恢复 / 生成码 / 客户建档。

所有写操作都在同一事务内完成，并写一条 ``audit_log``（INV-5），
``days_delta`` 与操作一致（缩期为负）。

期限调整的两道硬保护：
1. ``expires_at < issued_at`` → 409 ``expiry_too_early``（**force 也不能越过**）；
2. ``expires_at < now``（客户下次心跳立刻断服）→ 409 ``expiry_in_past``，
   需 ``force=true`` 二次确认。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from app import db, signing
from app.config import get_config
from app.errors import ApiError
from app.repo import audit, codes, customers, licenses, stats

logger = logging.getLogger(__name__)

#: 天数调整上限（超出视为非法）
_MAX_DAYS = 3650


def _actor_fields(actor: Dict[str, Any]) -> Dict[str, Any]:
    """从当前用户字典里取审计需要的字段。"""
    return {"actor": str(actor.get("username") or ""),
            "actor_id": int(actor.get("id") or 0)}


# ---------------------------------------------------------------------------
# 期限调整
# ---------------------------------------------------------------------------
def extend(*, license_id: str, days: int, force: bool, actor: Dict[str, Any],
           ip: str) -> Dict[str, Any]:
    """增减天数（``days > 0`` 增加、``days < 0`` 减少）。

    增加时基准是 ``max(旧到期日, 今天)``（与参考实现一致：客户提前续费不损失天数）。

    Raises:
        ApiError: not_found(404) / invalid_days(400) / expiry_too_early(409) /
            expiry_in_past(409)。
    """
    delta = int(days)
    if delta == 0 or abs(delta) > _MAX_DAYS:
        raise ApiError("invalid_days",
                       f"天数必须在 -{_MAX_DAYS} ~ {_MAX_DAYS} 之间且不能为 0", 400)
    moment = signing.now()

    with db.transaction() as cur:
        record = licenses.find_for_update(cur, license_id)
        if record is None:
            raise ApiError("not_found", f"许可证不存在：{license_id}", 404)

        old_expires: datetime = record["expires_at"]
        issued_at: datetime = record["issued_at"]
        base = max(old_expires, moment) if delta > 0 else old_expires
        new_expires = base + timedelta(days=delta)

        if new_expires < issued_at:
            raise ApiError(
                "expiry_too_early",
                f"调整后到期日 {new_expires:%Y-%m-%d} 不能早于签发日 "
                f"{issued_at:%Y-%m-%d}", 409)
        if new_expires < moment and not force:
            raise ApiError(
                "expiry_in_past",
                "调整后授权立即失效，客户下次心跳就会断服；确认请勾选强制", 409)

        licenses.set_expiry(cur, record["id"], new_expires, moment)
        # 动作统一记 extend（缩期靠 days_delta 的负值表达，见设计文档 §5.3 B7）
        audit.write(cur, **_actor_fields(actor), now=moment, action="extend",
                    target_type="license", target_id=str(record["license_id"]),
                    machine_code=str(record["machine_code"]),
                    days_delta=delta,
                    before={"expires_at": signing.to_iso(old_expires)},
                    after={"expires_at": signing.to_iso(new_expires),
                           "force": bool(force)},
                    ip=ip,
                    note=("延期" if delta > 0 else "缩期")
                         + f" {abs(delta)} 天")
        logger.info("期限调整：%s %+d 天 → %s（操作人 %s）", record["license_id"],
                    delta, new_expires, actor.get("username"))

    return {
        "ok": True,
        "item": {
            "license_id": str(license_id),
            "expires_at": signing.to_iso(new_expires),
            "days_delta": delta,
        },
    }


def set_expiry(*, license_id: str, expires_at: str, force: bool,
               actor: Dict[str, Any], ip: str) -> Dict[str, Any]:
    """直接设定到期日。

    入参支持 ``YYYY-MM-DD``（解释为当天 23:59:59 东八区）与完整 ISO8601。

    Raises:
        ApiError: not_found(404) / invalid_expires_at(400) /
            expiry_too_early(409) / expiry_in_past(409)。
    """
    parsed = signing.parse_expiry_text(expires_at)
    if parsed is None:
        raise ApiError("invalid_expires_at",
                       "到期日格式不正确（应为 YYYY-MM-DD 或完整 ISO8601）", 400)
    moment = signing.now()

    with db.transaction() as cur:
        record = licenses.find_for_update(cur, license_id)
        if record is None:
            raise ApiError("not_found", f"许可证不存在：{license_id}", 404)
        old_expires: datetime = record["expires_at"]
        issued_at: datetime = record["issued_at"]
        if parsed < issued_at:
            raise ApiError(
                "expiry_too_early",
                f"到期日 {parsed:%Y-%m-%d} 不能早于签发日 {issued_at:%Y-%m-%d}",
                409)
        if parsed < moment and not force:
            raise ApiError(
                "expiry_in_past",
                "设定的到期日已过，客户下次心跳就会断服；确认请勾选强制", 409)

        licenses.set_expiry(cur, record["id"], parsed, moment)
        days_delta = (parsed - old_expires).days
        audit.write(cur, **_actor_fields(actor), now=moment,
                    action="set_expiry", target_type="license",
                    target_id=str(record["license_id"]),
                    machine_code=str(record["machine_code"]),
                    days_delta=days_delta,
                    before={"expires_at": signing.to_iso(old_expires)},
                    after={"expires_at": signing.to_iso(parsed),
                           "force": bool(force)},
                    ip=ip, note=f"直接设定到期日 {parsed:%Y-%m-%d}")
        logger.info("设定到期日：%s → %s（操作人 %s）", record["license_id"],
                    parsed, actor.get("username"))

    return {
        "ok": True,
        "item": {
            "license_id": str(license_id),
            "expires_at": signing.to_iso(parsed),
            "days_delta": days_delta,
        },
    }


# ---------------------------------------------------------------------------
# 吊销 / 恢复
# ---------------------------------------------------------------------------
def revoke(*, license_id: str, reason: str, force: bool, actor: Dict[str, Any],
           ip: str) -> Dict[str, Any]:
    """吊销许可证（``revoked_at`` 非空 + ``current_key`` 置 NULL）。

    Raises:
        ApiError: not_found(404) / already_revoked(409) /
            superseded_require_force(409)。
    """
    moment = signing.now()
    with db.transaction() as cur:
        record = licenses.find_for_update(cur, license_id)
        if record is None:
            raise ApiError("not_found", f"许可证不存在：{license_id}", 404)
        if record.get("revoked_at"):
            raise ApiError(
                "already_revoked",
                f"该许可证已于 {signing.fmt_cn(record['revoked_at'])} 吊销，无需重复操作",
                409)
        machine = str(record["machine_code"])
        if not force:
            other = licenses.has_other_active(cur, machine,
                                             exclude_license_id=license_id)
            if other is not None:
                raise ApiError(
                    "superseded_require_force",
                    f"该机器已有更新的生效授权 {other['license_id']}，"
                    f"吊销旧证无意义；确认请勾选强制", 409)

        licenses.revoke(cur, record["id"], moment)
        audit.write(cur, **_actor_fields(actor), now=moment, action="revoke",
                    target_type="license", target_id=str(record["license_id"]),
                    machine_code=machine, days_delta=0,
                    before={"revoked_at": None, "current_key": machine,
                            "expires_at": signing.to_iso(record["expires_at"])},
                    after={"revoked_at": signing.to_iso(moment),
                           "current_key": None},
                    ip=ip, note=str(reason or ""))
        logger.warning("吊销授权：%s（机器码 %s，操作人 %s，原因 %s）",
                       record["license_id"], machine, actor.get("username"),
                       reason or "-")

    return {
        "ok": True,
        "item": {
            "license_id": str(license_id),
            "machine_code": machine,
            "revoked_at": signing.to_iso(moment),
        },
    }


def restore(*, license_id: str, actor: Dict[str, Any], ip: str) -> Dict[str, Any]:
    """撤销吊销（客户下次心跳即恢复）。

    Raises:
        ApiError: not_found(404) / not_revoked(409) / already_active(409)。
    """
    moment = signing.now()
    with db.transaction() as cur:
        record = licenses.find_for_update(cur, license_id)
        if record is None:
            raise ApiError("not_found", f"许可证不存在：{license_id}", 404)
        if not record.get("revoked_at"):
            raise ApiError("not_revoked", "该许可证当前未被吊销，无需恢复", 409)
        machine = str(record["machine_code"])
        other = licenses.has_other_active(cur, machine)
        if other is not None:
            raise ApiError(
                "already_active",
                f"该机器码已有生效授权 {other['license_id']}，无需恢复", 409)

        licenses.restore(cur, record["id"], machine, moment)
        audit.write(cur, **_actor_fields(actor), now=moment, action="restore",
                    target_type="license", target_id=str(record["license_id"]),
                    machine_code=machine, days_delta=0,
                    before={"revoked_at": signing.to_iso(record["revoked_at"]),
                            "current_key": None},
                    after={"revoked_at": None, "current_key": machine},
                    ip=ip, note="撤销吊销")
        logger.info("恢复授权：%s（机器码 %s，操作人 %s）",
                    record["license_id"], machine, actor.get("username"))

    return {
        "ok": True,
        "item": {
            "license_id": str(license_id),
            "machine_code": machine,
            "revoked_at": None,
        },
    }


# ---------------------------------------------------------------------------
# 激活码 / 客户
# ---------------------------------------------------------------------------
def create_codes(*, count: int, days: int, customer: str, customer_phone: str,
                 edition: str, note: str, batch: str,
                 actor: Dict[str, Any], ip: str) -> Dict[str, Any]:
    """批量生成激活码（带手机号时联动客户档案）。"""
    moment = signing.now()
    cfg = get_config()
    with db.transaction() as cur:
        result = codes.batch_create(
            cur, count=count, days=days,
            edition=edition or cfg.default_edition,
            customer_name=customer, customer_phone=customer_phone,
            note=note, batch=batch,
            created_by=str(actor.get("username") or ""), now=moment)
        audit.write(cur, **_actor_fields(actor), now=moment,
                    action="create_codes", target_type="code",
                    target_id=result["batch"],
                    machine_code="", days_delta=0, before=None,
                    after={"batch": result["batch"],
                           "count": len(result["items"]),
                           "days": int(days),
                           "customer": customer,
                           "customer_phone": customer_phone},
                    ip=ip, note=f"生成 {len(result['items'])} 个激活码")
        logger.info("生成激活码 %d 个（批次 %s，%d 天，客户 %s，操作人 %s）",
                    len(result["items"]), result["batch"], int(days),
                    customer or "-", actor.get("username"))
    return {"ok": True, "batch": result["batch"], "items": result["items"]}


def upsert_customer(*, name: str, phone: str, note: str,
                    actor: Dict[str, Any], ip: str) -> Dict[str, Any]:
    """新增 / 更新客户档案（按手机号 upsert）。"""
    phone_text = customers.normalize_phone(phone)
    if not phone_text:
        raise ApiError("invalid_param", "手机号不能为空", 400)
    moment = signing.now()
    with db.transaction() as cur:
        existing = None
        if phone_text:
            cur.execute("SELECT * FROM customer WHERE phone = %s LIMIT 1",
                        (phone_text,))
            existing = cur.fetchone()
        profile = customers.upsert_by_phone(cur, phone_text, name, moment)
        if note:
            cur.execute("UPDATE customer SET note = %s, updated_at = %s "
                        "WHERE id = %s", (str(note)[:255], moment,
                                          int(profile["id"])))
        audit.write(cur, **_actor_fields(actor), now=moment,
                    action="create_customer" if existing is None
                    else "update_customer",
                    target_type="customer", target_id=str(profile["id"]),
                    machine_code="", days_delta=0,
                    before=({"name": existing.get("name"),
                             "phone": existing.get("phone"),
                             "note": existing.get("note")}
                            if existing else None),
                    after={"name": profile.get("name"),
                           "phone": profile.get("phone"),
                           "note": str(note or "")},
                    ip=ip, note="客户档案维护")
    return {"ok": True, "item": profile}


# ---------------------------------------------------------------------------
# 仪表盘
# ---------------------------------------------------------------------------
def dashboard(days: int = 30) -> Dict[str, Any]:
    """仪表盘全部统计口径（设计文档 §6）。"""
    span = max(7, min(int(days or 30), 90))
    moment = signing.now()
    with db.transaction() as cur:
        data = stats.dashboard(cur, moment, span)
    data["ok"] = True
    return data


def meta() -> Dict[str, Any]:
    """管理台渲染用字典（版本选项、默认天数、限流参数）。"""
    cfg = get_config()
    return {
        "ok": True,
        "editions": ["standard", "pro", "trial"],
        "default_edition": cfg.default_edition,
        "default_code_days": cfg.default_code_days,
        "default_grace_days": cfg.grace_days,
        "features_default": cfg.features_default,
        "key_id": cfg.key_id,
        "rate_limits": {
            "activate": "%d/%d" % cfg.activate_rate_limit,
            "login": "%d/%d" % cfg.login_rate_limit,
            "query": "%d/%d" % cfg.query_rate_limit,
        },
        "session_ttl_hours": cfg.session_ttl_hours,
        "server_time": signing.now(),
    }
