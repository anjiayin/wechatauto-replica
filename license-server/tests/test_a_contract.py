# -*- coding: utf-8 -*-
"""A 组：客户端契约一致性（最高优先级）。

对齐依据：``kefu-system/docs/授权服务接口文档.md``、``kefu-system/app/license.py``、
``docs/design.md`` §5.1。
所有验签都用**客户端自己的代码**（子进程调用），不复用服务端实现。
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import pytest

import _helpers as H


# ---------------------------------------------------------------------------
# A1/A2 激活签发 + 客户端验签 + 签名原文逐字节一致
# ---------------------------------------------------------------------------
def test_a01_activate_payload_contract(admin):
    """A1：签发的许可证能被客户端 verify_license 验签通过，字段与请求一致。"""
    code = H.create_codes(admin, count=1, days=365,
                          customer="某某汽配", phone="13900001111")[0]
    resp = H.activate(H.M1, code)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data.get("ok") is True

    doc = data["license"]
    assert set(doc) == {"key_id", "payload", "signature"}, doc.keys()
    payload = doc["payload"]

    # 机器码：请求用的就是分组大写形式 → payload 必须逐字一致
    assert payload["machine_code"] == H.M1

    # 时间口径：必须带 +08:00
    assert H.is_iso_cn(payload["issued_at"]), payload["issued_at"]
    assert H.is_iso_cn(payload["expires_at"]), payload["expires_at"]
    assert H.parse_iso(payload["expires_at"]) > H.parse_iso(payload["issued_at"])

    # 其余字段类型/取值
    assert payload["customer"] == "某某汽配"
    assert payload["edition"] == "standard"
    assert isinstance(payload["features"], list) and payload["features"]
    assert payload["grace_days"] == 7
    assert str(payload["license_id"]).startswith("LIC-")

    # 客户端验签
    ok, reason = H.verify_with_client(doc)
    assert ok is True, "客户端验签失败：%s" % reason


def test_a02_payload_bytes_byte_identical(admin):
    """A2：服务端 payload_bytes 与客户端 payload_bytes 逐字节相同（比 sha256）。"""
    code = H.create_codes(admin, count=1, days=180,
                          customer="含中文&特殊字符<>\u0026", phone="13900002222")[0]
    doc = H.activate_ok(H.M2, code)
    payload = doc["payload"]

    server_bytes = H.signing.payload_bytes(payload)
    client = H.client_side(payload=payload, doc=doc)

    assert client["bytes_hex"] == server_bytes.hex(), (
        "签名原文不一致\n服务端：%r\n客户端：%r"
        % (server_bytes, bytes.fromhex(client["bytes_hex"])))
    assert client["bytes_sha256"] == hashlib.sha256(server_bytes).hexdigest()
    assert client["verify_ok"] is True, client.get("verify_reason")


def test_a03_activate_accepts_compact_machine_code(admin):
    """机器码两种写法（紧凑/分组）落到同一条证上。"""
    code = H.create_codes(admin, count=1, days=30)[0]
    doc = H.activate_ok(H.compact(H.M3), code)
    assert doc["payload"]["machine_code"] == H.M3

    rows = H.q("SELECT machine_code, current_key FROM license")
    assert len(rows) == 1
    assert rows[0]["machine_code"] == H.M3
    assert rows[0]["current_key"] == H.M3


# ---------------------------------------------------------------------------
# A3 心跳：每次都带新 license，延期后 expires_at 变、issued_at 不变
# ---------------------------------------------------------------------------
def test_a03_heartbeat_returns_fresh_license(admin):
    """A3：连续两次心跳都带 license；管理端延期后第二次的 expires_at 必须变。"""
    code = H.create_codes(admin, count=1, days=100, phone="13900003333")[0]
    doc = H.activate_ok(H.M1, code)
    lic_id = doc["payload"]["license_id"]

    first = H.heartbeat(H.M1, lic_id)
    assert first.status_code == 200, first.text
    body1 = first.json()
    assert body1["ok"] is True and body1["status"] == "active"
    assert body1["license_id"] == lic_id
    hb_doc1 = body1["license"]
    assert H.verify_with_client(hb_doc1)[0] is True
    assert H.is_iso_cn(body1["expires_at"])

    # 管理端延期 30 天
    resp = admin.post("/admin/api/licenses/%s/extend" % lic_id, {"days": 30})
    assert resp.status_code == 200, resp.text

    second = H.heartbeat(H.M1, lic_id)
    body2 = second.json()
    assert body2["ok"] is True
    hb_doc2 = body2["license"]
    assert H.verify_with_client(hb_doc2)[0] is True, "延期后心跳签发的证验签失败"

    p1, p2 = hb_doc1["payload"], hb_doc2["payload"]
    assert p2["issued_at"] == p1["issued_at"], "心跳重签不允许改 issued_at（INV-4）"
    assert p2["expires_at"] != p1["expires_at"], "延期后 expires_at 没有变化"
    assert (H.parse_iso(p2["expires_at"]) - H.parse_iso(p1["expires_at"])
            == timedelta(days=30))


# ---------------------------------------------------------------------------
# A4/A5 吊销 / 到期：必须 200 + ok=false（不能 4xx）
# ---------------------------------------------------------------------------
def test_a04_heartbeat_revoked_is_200_ok_false(admin):
    """A4：吊销后心跳必须 HTTP 200 + ok=false + code=revoked。"""
    code = H.create_codes(admin, count=1, days=100)[0]
    doc = H.activate_ok(H.M1, code)
    lic_id = doc["payload"]["license_id"]

    resp = admin.post("/admin/api/licenses/%s/revoke" % lic_id,
                      {"reason": "客户欠费"})
    assert resp.status_code == 200, resp.text

    hb = H.heartbeat(H.M1, lic_id)
    assert hb.status_code == 200, "吊销后心跳返回 %s（客户端会误判为服务器故障）" % hb.status_code
    body = hb.json()
    assert body.get("ok") is False, body
    assert H.error_of(hb).get("code") == "revoked", body
    assert H.has_chinese(H.error_of(hb).get("message"))


def test_a05_heartbeat_expired_is_200_ok_false(admin):
    """A5：到期后心跳 200 + ok=false + code=expired。"""
    code = H.create_codes(admin, count=1, days=365)[0]
    doc = H.activate_ok(H.M1, code)
    lic_id = doc["payload"]["license_id"]

    # 时间旅行：把签发/到期都推到过去（等价于"证自然到期"）
    now = H.signing.now()
    H.x("UPDATE license SET issued_at = %s, expires_at = %s WHERE license_id = %s",
        (now - timedelta(days=30), now - timedelta(days=1), lic_id))

    hb = H.heartbeat(H.M1, lic_id)
    assert hb.status_code == 200, hb.status_code
    assert hb.json().get("ok") is False, hb.json()
    assert H.error_of(hb).get("code") == "expired", hb.text
    assert H.has_chinese(H.error_of(hb).get("message"))


# ---------------------------------------------------------------------------
# A6 七个错误码：统一顶层 error 结构 + 中文 message + 客户端 extract_error 认得出
# ---------------------------------------------------------------------------
def _assert_error(resp, status: int, code: str) -> None:
    """统一断言：HTTP 码、顶层 error 结构、中文 message、客户端解析一致。"""
    assert resp.status_code == status, "%s → %s %s" % (code, resp.status_code, resp.text)
    body = resp.json()
    assert "error" in body and isinstance(body["error"], dict), body
    err = body["error"]
    assert set(err) == {"code", "message"}, err
    assert err["code"] == code, err
    assert H.has_chinese(err["message"]), err
    # 客户端的 extract_error 必须能从该体里取到同一个 code（前后端契约）
    parsed = H.client_side(error_body=body)
    assert parsed["error_code"] == code, parsed
    assert parsed["error_message"] == err["message"], parsed


def test_a06_error_invalid_machine_code(admin):
    """A6-1：机器码非 32 位十六进制 → 400 invalid_machine_code。"""
    resp = H.activate("XYZ", "KF-1111-1111-1111-1111")
    _assert_error(resp, 400, "invalid_machine_code")


def test_a06_error_activation_required(admin):
    """A6-2：无生效证且不带激活码 → 400 activation_required。"""
    resp = H.activate(H.M2, "")
    _assert_error(resp, 400, "activation_required")


def test_a06_error_invalid_code(admin):
    """A6-3：激活码不存在 → 400 invalid_code。"""
    resp = H.activate(H.M2, "KF-0000-0000-0000-0000")
    _assert_error(resp, 400, "invalid_code")


def test_a06_error_code_used(admin):
    """A6-4：码已被其它机器用掉 → 409 code_used。"""
    code = H.create_codes(admin, count=1, days=30)[0]
    H.activate_ok(H.M1, code)
    resp = H.activate(H.M2, code)
    _assert_error(resp, 409, "code_used")


def test_a06_error_revoked(admin):
    """A6-5：吊销后拿自己的旧码自助解封 → 400 revoked。"""
    code = H.create_codes(admin, count=1, days=30)[0]
    doc = H.activate_ok(H.M1, code)
    lic_id = doc["payload"]["license_id"]
    assert admin.post("/admin/api/licenses/%s/revoke" % lic_id,
                      {"reason": "退款"}).status_code == 200
    resp = H.activate(H.M1, code)
    _assert_error(resp, 400, "revoked")


def test_a06_error_not_found(admin):
    """A6-6：心跳查不到任何记录 → 404 not_found。"""
    resp = H.heartbeat(H.M3, "LIC-19700101-0000")
    _assert_error(resp, 404, "not_found")


def test_a06_error_machine_mismatch(admin):
    """A6-7：心跳带的 license_id 属于另一台机器 → 409 machine_mismatch。"""
    code = H.create_codes(admin, count=1, days=30)[0]
    doc = H.activate_ok(H.M1, code)
    lic_id = doc["payload"]["license_id"]
    resp = H.heartbeat(H.M2, lic_id)
    _assert_error(resp, 409, "machine_mismatch")


def test_a06_error_body_json_printable():
    """错误体必须是合法 JSON（客户端 json.loads 不炸）。"""
    resp = H.activate("XYZ")
    assert resp.headers.get("content-type", "").startswith("application/json")
    json.loads(resp.text)


# ---------------------------------------------------------------------------
# A7 查询接口（C3）契约
# ---------------------------------------------------------------------------
def test_a07_query_returns_payload_and_revoked_flag(admin):
    """A7：/api/v1/license/query 回 payload + revoked 标记（客服核对用）。"""
    code = H.create_codes(admin, count=1, days=30, customer="查询客户",
                          phone="13900007777")[0]
    doc = H.activate_ok(H.M1, code)

    resp = H.anon().get("/api/v1/license/query",
                        params={"machine_code": H.compact(H.M1)}, csrf=False)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["revoked"] is False
    payload = body["license"]
    assert payload["machine_code"] == H.M1
    assert payload["license_id"] == doc["payload"]["license_id"]
    assert H.is_iso_cn(payload["expires_at"])

    # 吊销后 revoked 必须为 true（且仍返回 payload，便于客服核对历史）
    assert admin.post("/admin/api/licenses/%s/revoke" % payload["license_id"],
                      {"reason": "查询测试"}).status_code == 200
    body = H.anon().get("/api/v1/license/query",
                        params={"machine_code": H.compact(H.M1)},
                        csrf=False).json()
    assert body["ok"] is True and body["revoked"] is True, body
