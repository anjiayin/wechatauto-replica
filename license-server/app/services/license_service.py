# -*- coding: utf-8 -*-
"""客户端授权业务（activate / heartbeat / query）。

业务语义与参考实现 ``kefu-system/tools/mock_license_server.py`` **等价**，
并按设计文档 §11-2 修正了它的幂等缺陷：

* 同一台机器重复提交**同一个**已用激活码 → 到期日**不叠加**，直接返回原证
  （客户重装软件/重装系统会重复激活，叠加会白送天数）；
* 换一个**新码** → 从 ``max(旧到期日, 今天)`` 起叠加（客户买第二次可直接续期）；
* 已吊销机器只认**未使用过**的新码（堵死"拿吊销前的旧码自助解封"）。

心跳的四步判定顺序（与参考实现一致，**不要改**）：
``not_found(404)`` → ``machine_mismatch(409)`` → ``revoked(200/ok=false)`` →
更新 ``last_seen_at`` → ``expired(200/ok=false)`` → ``200/ok=true``。
授权失效一律用 **HTTP 200 + ok=false**：客户端把 4xx 当成"服务器暂不可用"，
只推迟确认而不改变授权状态。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from app import db, signing
from app.config import get_config
from app.errors import ApiError, error_body
from app.repo import audit, codes, customers, licenses

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _require_machine(value: Any) -> str:
    """归一化并校验机器码；非法抛 400 ``invalid_machine_code``。"""
    machine = signing.normalize_machine(value)
    if not signing.valid_machine_code(machine):
        raise ApiError("invalid_machine_code",
                       "机器码格式不正确（应为 32 位十六进制）", 400)
    return machine


def _new_unique_license_id(cur: Any, moment: datetime) -> str:
    """生成库内不存在的许可证号（``LIC-yyyymmdd-XXXX``，最多试 5 次）。"""
    for _ in range(5):
        candidate = signing.new_license_id(moment)
        if licenses.find_by_license_id(cur, candidate) is None:
            return candidate
    raise ApiError("internal_error", "生成许可证号失败（随机冲突），请重试", 500)


def _sign(record: Dict[str, Any]) -> Dict[str, Any]:
    """按当前配置给许可证记录签名（每次下发都重新签，期限永远最新）。"""
    cfg = get_config()
    payload = signing.build_payload(record, cfg.grace_days,
                                    cfg.features_default)
    return signing.sign_doc(payload, signing.get_private_key(), cfg.key_id)


def _code_days(item: Dict[str, Any]) -> int:
    """激活码对应的有效天数（异常数据兜底默认值）。"""
    cfg = get_config()
    try:
        days = int(item.get("days") or 0)
    except (TypeError, ValueError):
        days = 0
    return days if days > 0 else cfg.default_code_days


# ---------------------------------------------------------------------------
# 激活
# ---------------------------------------------------------------------------
def activate(*, machine_code: str, activation_code: str, app_version: str,
             os_name: str, ip: str) -> Dict[str, Any]:
    """激活（首次 / 幂等续期 / 吊销后重发）。

    Args:
        machine_code: 客户端上报的机器码（可带横杠，统一归一化）。
        activation_code: 激活码，空串表示"不带激活码"。
        app_version / os_name: 客户端版本与系统描述（排查用）。
        ip: 客户端 IP（只用于日志与审计）。

    Returns:
        ``{"ok": True, "license": {"key_id","payload","signature"}}``。

    Raises:
        ApiError: invalid_machine_code / activation_required / invalid_code /
            code_used / revoked / conflict。
    """
    cfg = get_config()
    machine = _require_machine(machine_code)
    code = signing.normalize_code(activation_code)
    moment = signing.now()

    with db.transaction() as cur:
        current = licenses.lock_current(cur, machine)

        # ---------------- 分支 1：已有生效证（幂等 / 续期叠加） ----------------
        if current is not None:
            if code:
                item = codes.find_by_code_for_update(cur, code)
                if item is None:
                    raise ApiError("invalid_code", "激活码不存在，请核对后重试", 400)
                used_machine = str(item.get("used_machine") or "")
                if used_machine and used_machine != machine:
                    raise ApiError("code_used", "该激活码已被其它电脑使用", 409)
                same_code = (used_machine == machine
                             and str(current.get("activation_code") or "") == code)
                if same_code:
                    # 幂等修正：同一张证 + 同一个码重复提交，不再叠加天数
                    logger.info("重复激活（幂等，不叠加）：机器码 %s 激活码 %s",
                                machine, code)
                else:
                    base = (current["expires_at"]
                            if current["expires_at"] > moment else moment)
                    days = _code_days(item)
                    new_expires = base + timedelta(days=days)
                    profile = customers.upsert_by_phone(
                        cur, item.get("customer_phone"),
                        item.get("customer_name"), moment)
                    licenses.update_for_renew(cur, current["id"], new_expires,
                                              code, profile, item, moment)
                    codes.mark_used(cur, code, machine, moment)
                    audit.write(cur, now=moment, actor="client",
                                action="renew_via_activate",
                                target_type="license",
                                target_id=str(current["license_id"]),
                                machine_code=machine, days_delta=days,
                                before={"expires_at": signing.to_iso(
                                    current["expires_at"])},
                                after={"expires_at": signing.to_iso(new_expires),
                                       "activation_code": code},
                                ip=ip, note=f"客户端激活接口续期 {days} 天")
                    refreshed = licenses.find_by_license_id(
                        cur, current["license_id"])
                    current = refreshed or current
                    logger.info("激活续期：机器码 %s 新到期 %s（+%d 天，码 %s）",
                                machine, new_expires, days, code)
            else:
                logger.info("重复激活（幂等，未带激活码）：机器码 %s 许可证 %s",
                            machine, current["license_id"])
            return {"ok": True, "license": _sign(current)}

        # ---------------- 分支 2：无生效证，必须先有激活码 ----------------
        if not code:
            raise ApiError("activation_required",
                           "需要激活码：请把机器码发给供应商换取", 400)

        item = codes.find_by_code_for_update(cur, code)
        if item is None:
            raise ApiError("invalid_code", "激活码不存在，请核对后重试", 400)
        used_machine = str(item.get("used_machine") or "")
        if used_machine and used_machine != machine:
            raise ApiError("code_used", "该激活码已被其它电脑使用", 409)

        latest = licenses.find_latest(cur, machine)
        reissued_from = ""
        if latest is not None and latest.get("revoked_at"):
            if used_machine:
                # 拿自己用过的旧码自助解封 → 拒绝
                logger.warning("已吊销机器尝试用旧码自助解封：机器码 %s 码 %s",
                               machine, code)
                raise ApiError("revoked",
                               "该机器码的授权已被吊销，需要供应商重新发一个激活码",
                               400)
            reissued_from = str(latest.get("license_id") or "")

        # ---------------- 分支 3/4：首次激活 或 吊销后重发 ----------------
        days = _code_days(item)
        issued = moment
        expires = issued + timedelta(days=days)
        profile = customers.upsert_by_phone(cur, item.get("customer_phone"),
                                            item.get("customer_name"), moment)
        license_id = _new_unique_license_id(cur, issued)
        record = licenses.insert(
            cur,
            license_id=license_id,
            machine_code=machine,
            issued_at=issued,
            expires_at=expires,
            customer_id=int(profile.get("id") or 0),
            customer_name=str(profile.get("name")
                              or item.get("customer_name") or ""),
            customer_phone=str(profile.get("phone")
                               or item.get("customer_phone") or ""),
            edition=str(item.get("edition") or cfg.default_edition),
            activation_code=code,
            grace_days=cfg.grace_days,
            features=cfg.features_default,
            reissued_from=reissued_from,
            last_seen_at=issued,
            app_version=app_version,
            os_name=os_name,
            note=str(item.get("note") or ""),
            now=moment,
        )
        codes.mark_used(cur, code, machine, issued)
        audit.write(cur, now=moment, actor="client",
                    action="activate_reissue" if reissued_from else "activate_first",
                    target_type="license", target_id=license_id,
                    machine_code=machine, days_delta=days,
                    before={"reissued_from": reissued_from} if reissued_from else None,
                    after={"license_id": license_id,
                           "expires_at": signing.to_iso(expires),
                           "activation_code": code},
                    ip=ip,
                    note="吊销后重新授权" if reissued_from else "首次激活")
        logger.info("激活成功：机器码 %s 许可证 %s 到期 %s（码 %s）%s",
                    machine, license_id, expires, code,
                    f"（原证 {reissued_from}）" if reissued_from else "")
        return {"ok": True, "license": _sign(record)}


# ---------------------------------------------------------------------------
# 心跳
# ---------------------------------------------------------------------------
def heartbeat(*, machine_code: str, license_id: str, app_version: str,
              ip: str = "") -> Dict[str, Any]:
    """联网确认。

    Returns:
        正常：``{"ok": True, "status": "active", "license_id", "expires_at",
        "license"}``；授权失效：``{"ok": False, "error": {...}}``（HTTP 一律 200）。

    Raises:
        ApiError: not_found(404) / machine_mismatch(409) /
            invalid_machine_code(400)。
    """
    machine = _require_machine(machine_code)
    requested_id = str(license_id or "").strip()
    moment = signing.now()

    with db.transaction() as cur:
        requested = (licenses.find_by_license_id(cur, requested_id)
                     if requested_id else None)
        if requested is not None and requested.get("machine_code") != machine:
            raise ApiError("machine_mismatch", "许可证绑定的机器码与本机不一致", 409)

        current = licenses.find_current(cur, machine)
        if current is None:
            latest = requested or licenses.find_latest(cur, machine)
            if latest is None:
                raise ApiError("not_found", "服务器上没有这台电脑的授权记录", 404)
            if latest.get("machine_code") != machine:
                raise ApiError("machine_mismatch", "许可证绑定的机器码与本机不一致",
                               409)
            if latest.get("revoked_at"):
                # 吊销后不再刷新 last_seen_at（活跃统计只算可用机器）
                logger.info("心跳被拒（已吊销）：机器码 %s 许可证 %s", machine,
                            latest.get("license_id"))
                return error_body(
                    "revoked",
                    f"授权已于 {signing.fmt_cn(latest.get('revoked_at'))} 被吊销，"
                    f"请联系供应商")
            current = latest

        licenses.touch_seen(cur, current["id"], moment, app_version)
        current["last_seen_at"] = moment
        current["app_version"] = str(app_version or "")[:32]

        if current["expires_at"] <= moment:
            logger.info("心跳被拒（已到期）：机器码 %s 到期 %s", machine,
                        current["expires_at"])
            return error_body("expired", "授权已到期，请续费")

        doc = _sign(current)
        return {
            "ok": True,
            "status": "active",
            "license_id": str(current["license_id"]),
            "expires_at": signing.to_iso(current["expires_at"]),
            "license": doc,
        }


# ---------------------------------------------------------------------------
# 查询（客服核对用，客户端不调用）
# ---------------------------------------------------------------------------
def query(*, machine_code: str) -> Dict[str, Any]:
    """查某机器码的授权（不签名，只回 payload）。"""
    machine = _require_machine(machine_code)
    with db.transaction() as cur:
        record: Optional[Dict[str, Any]] = licenses.find_current(cur, machine)
        if record is None:
            record = licenses.find_latest(cur, machine)
        if record is None:
            return error_body("not_found", "无授权记录")
        payload = signing.build_payload(record, get_config().grace_days,
                                        get_config().features_default)
        return {
            "ok": True,
            "license": payload,
            "revoked": bool(record.get("revoked_at")),
            "role": _role(record, signing.now()),
        }


def _role(record: Dict[str, Any], moment: datetime) -> str:
    """记录的角色标记（当前生效 / 已过期 / 已吊销 / 已被取代）。"""
    return licenses.annotate_role(record, moment)["role"]
