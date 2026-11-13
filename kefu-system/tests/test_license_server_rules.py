# -*- coding: utf-8 -*-
"""模拟授权服务器的业务规则测试（激活码绑定 / 吊销 / 重新授权 / 恢复）。

用进程内 TestClient + 临时数据文件，不联网、不碰 tools/mock_license_data.json。

背景（2026-11-13 现场）：客户机器被吊销过之后，用一个**全新的未使用激活码**
去激活，服务端在"旧记录已吊销"处直接拒了 —— 供应商没法重新授权同一台机器。
这里把规则钉死：旧码不许自助解封，新码必须放行。

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_license_server_rules.py -q --basetemp=D:/tmp/pt-licsrv
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR / "tools") not in sys.path:
    sys.path.insert(0, str(BASE_DIR / "tools"))

import mock_license_server as srv  # noqa: E402  （必须在 sys.path 调整后导入）

MACHINE_A = "AAAA-BBBB-CCCC-DDDD-EEEE-FFFF-1111-2222"
MACHINE_B = "9999-8888-7777-6666-5555-4444-3333-2222"


@pytest.fixture()
def server(tmp_path):
    """创建一套独立的模拟服务器（临时数据文件 + 临时密钥）。"""
    app = srv.create_app(Ed25519PrivateKey.generate(), "test",
                         tmp_path / "license.json")
    return TestClient(app)


def make_code(server: TestClient, days: int = 30,
              customer: str = "测试客户") -> str:
    """通过管理接口生成一个激活码。"""
    resp = server.post("/admin/api/codes", json={
        "count": 1, "days": days, "customer": customer})
    assert resp.status_code == 200, resp.text
    return resp.json()["items"][0]["code"]


def activate(server: TestClient, code: str,
             machine: str = MACHINE_A) -> dict:
    """调用激活接口，返回许可证 payload（失败时抛断言）。"""
    resp = server.post("/api/v1/license/activate", json={
        "machine_code": machine, "activation_code": code})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    return body["license"]["payload"]


def err_code(resp) -> str:
    """取错误码（服务端已摊平为顶层 error）。"""
    return str((resp.json().get("error") or {}).get("code") or "")


def test_new_code_activates_and_binds_machine(server):
    """全新激活码：直接激活并绑定机器码。"""
    code = make_code(server)
    payload = activate(server, code)
    assert payload["machine_code"] == MACHINE_A
    assert payload["customer"] == "测试客户"
    assert payload["license_id"].startswith("LIC-")


def test_code_cannot_be_used_by_another_machine(server):
    """一个激活码只能给一台机器（换机报 code_used）。"""
    code = make_code(server)
    activate(server, code)
    resp = server.post("/api/v1/license/activate", json={
        "machine_code": MACHINE_B, "activation_code": code})
    assert resp.status_code == 400
    assert err_code(resp) == "code_used"


def test_same_machine_with_new_code_extends(server):
    """同一台机器再买一次：从原到期日叠加，不重置。"""
    first = activate(server, make_code(server, days=30))
    second = activate(server, make_code(server, days=30))
    assert second["license_id"] == first["license_id"]
    assert srv.parse_iso(second["expires_at"]) > srv.parse_iso(
        first["expires_at"])


def test_revoked_blocks_heartbeat(server):
    """吊销后联网确认必须失败。"""
    payload = activate(server, make_code(server))
    server.post(f"/admin/api/licenses/{payload['license_id']}/revoke")
    resp = server.post("/api/v1/license/heartbeat", json={
        "machine_code": MACHINE_A, "license_id": payload["license_id"]})
    body = resp.json()
    assert body["ok"] is False
    assert (body.get("error") or {}).get("code") == "revoked"


def test_revoked_machine_rejects_the_old_code(server):
    """吊销后拿"自己用过的旧码"重试 = 自助解封，必须拒绝。"""
    code = make_code(server)
    payload = activate(server, code)
    server.post(f"/admin/api/licenses/{payload['license_id']}/revoke")
    resp = server.post("/api/v1/license/activate", json={
        "machine_code": MACHINE_A, "activation_code": code})
    assert resp.status_code == 400
    assert err_code(resp) == "revoked"
    assert "重新发" in (resp.json()["error"].get("message") or "")


def test_revoked_machine_accepts_a_fresh_code(server):
    """吊销后供应商重新发码：必须能重新授权（本次现场问题的修复点）。"""
    old = activate(server, make_code(server))
    server.post(f"/admin/api/licenses/{old['license_id']}/revoke")

    new_code = make_code(server, days=365, customer="重新授权客户")
    fresh = activate(server, new_code)
    assert fresh["license_id"] != old["license_id"]
    assert fresh["customer"] == "重新授权客户"

    # 按机器码查必须返回新记录（旧的吊销记录只作历史）
    resp = server.get("/api/v1/license/query", params={
        "machine_code": MACHINE_A})
    body = resp.json()
    assert body["ok"] is True
    assert body["license"]["license_id"] == fresh["license_id"]
    assert body["revoked"] is False
    # 新证书能通过心跳（说明它是"生效"的那条）
    hb = server.post("/api/v1/license/heartbeat", json={
        "machine_code": MACHINE_A, "license_id": fresh["license_id"]})
    assert hb.json()["ok"] is True


def test_restore_after_mistaken_revoke(server):
    """误吊销可以撤销：恢复后心跳重新通过。"""
    payload = activate(server, make_code(server))
    server.post(f"/admin/api/licenses/{payload['license_id']}/revoke")
    resp = server.post(f"/admin/api/licenses/{payload['license_id']}/restore")
    assert resp.status_code == 200
    assert resp.json()["item"]["revoked_at"] == ""
    hb = server.post("/api/v1/license/heartbeat", json={
        "machine_code": MACHINE_A, "license_id": payload["license_id"]})
    assert hb.json()["ok"] is True


def test_restore_rejected_when_a_new_license_is_active(server):
    """已重新发码的机器不允许复活旧记录（避免一台机两条生效授权）。"""
    old = activate(server, make_code(server))
    server.post(f"/admin/api/licenses/{old['license_id']}/revoke")
    activate(server, make_code(server))
    resp = server.post(f"/admin/api/licenses/{old['license_id']}/restore")
    assert resp.status_code == 409
    assert err_code(resp) == "already_active"


def test_annotate_marks_active_and_superseded():
    """列表注释：最新未吊销的是「当前生效」，更早的同机记录是「已被新证取代」。

    现场看过一次误判：管理员看到旧吊销记录的到期日更晚，以为客户端状态错了。
    """
    items = [
        {"license_id": "L2", "machine_code": "M", "revoked_at": ""},
        {"license_id": "L1", "machine_code": "M", "revoked_at": ""},
        {"license_id": "L0", "machine_code": "M", "revoked_at": "2026-11-13"},
    ]
    rows = srv.annotate_licenses(items)
    assert [r["role"] for r in rows] == ["active", "superseded", "revoked"]
    # 该机器已有生效授权，旧吊销记录不能被"恢复"
    assert rows[2]["can_restore"] is False
    # 注释不能写回原始记录（否则会随 Store 落盘）
    assert all("role" not in item for item in items)


def test_admin_list_exposes_roles_newest_first(server):
    """管理接口：最新一条是 active，吊销的旧记录排在后面且不可恢复。"""
    old = activate(server, make_code(server))
    server.post(f"/admin/api/licenses/{old['license_id']}/revoke")
    new = activate(server, make_code(server))

    items = server.get("/admin/api/licenses").json()["items"]
    assert items[0]["license_id"] == new["license_id"]
    assert items[0]["role"] == "active"
    revoked = [i for i in items if i["license_id"] == old["license_id"]][0]
    assert revoked["role"] == "revoked"
    assert revoked["can_restore"] is False
