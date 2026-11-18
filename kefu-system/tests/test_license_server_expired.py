# -*- coding: utf-8 -*-
"""服务器明确判定过期（code="expired"）必须立即停用的回归测试。

背景：供应商在授权后台把许可证有效期调成已过期后，服务器心跳返回
``ok=false, error.code="expired"``。修复前客户端只记录 last_error，仍然按
「断网宽限」放行（现场实测踩过）。修复后语义对齐 ``revoked``：服务器任何
明确拒绝（expired/revoked）都停用，宽限只保留给网络不可达。

全部离线：HTTP 用替身/monkeypatch，不碰真实授权服务器。

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_license_server_expired.py -v --basetemp=D:/tmp/pt-lic-exp
"""
from __future__ import annotations

import base64
import io
import json
from datetime import timedelta
from urllib.error import HTTPError

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app import license as lic

MACHINE = "AAAA-BBBB-CCCC-DDDD-EEEE-FFFF-1111-2222"


# ---------------------------------------------------------------------------
# 工具（与 test_license_core.py 同款：临时密钥 + 注入式客户端）
# ---------------------------------------------------------------------------
@pytest.fixture()
def signer(monkeypatch):
    """生成一对临时密钥并把公钥注入 PUBLIC_KEYS（不依赖仓库里的 dev 密钥）。"""
    private_key = Ed25519PrivateKey.generate()
    raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    monkeypatch.setitem(lic.PUBLIC_KEYS, "test",
                        base64.b64encode(raw).decode("ascii"))
    return private_key


def make_doc(private_key, payload: dict) -> dict:
    """按客户端约定签一张许可证。"""
    signature = private_key.sign(lic.payload_bytes(payload))
    return {"key_id": "test", "payload": payload,
            "signature": base64.b64encode(signature).decode("ascii")}


def make_payload(**over) -> dict:
    """默认 payload（100 天后到期，绑定 MACHINE）。"""
    base = {
        "license_id": "LIC-TEST-0001",
        "machine_code": MACHINE,
        "customer": "测试客户",
        "edition": "standard",
        "issued_at": lic.now().isoformat(),
        "expires_at": (lic.now() + timedelta(days=100)).isoformat(),
        "grace_days": 7,
        "features": ["monitor", "reply", "ai"],
    }
    base.update(over)
    return base


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """启用授权 + 临时许可证/状态文件 + 固定机器码。"""
    monkeypatch.setenv("KEFU_LICENSE_ENABLED", "1")
    monkeypatch.setenv("KEFU_LICENSE_SERVER", "http://127.0.0.1:1")
    monkeypatch.delenv("KEFU_LICENSE_MODE", raising=False)

    def build(**kw) -> lic.LicenseClient:
        return lic.LicenseClient(
            machine_code_provider=kw.pop("machine", lambda: MACHINE),
            license_path=tmp_path / "license.json",
            state_path=tmp_path / "license.state.json",
            **kw)

    return type("Env", (), {
        "dir": tmp_path,
        "client": build(),
        "build": staticmethod(build),
        "license_path": tmp_path / "license.json",
        "state_path": tmp_path / "license.state.json",
    })


def write_doc(env, private_key, payload: dict) -> None:
    """把签名许可证写进临时目录。"""
    doc = make_doc(private_key, payload)
    env.license_path.write_text(json.dumps(doc, ensure_ascii=False),
                                encoding="utf-8")


def write_state(env, **fields) -> None:
    """写本地状态文件（模拟历史联网记录 / 停用标记 / 时钟基准）。"""
    env.state_path.write_text(json.dumps(fields, ensure_ascii=False),
                              encoding="utf-8")


def read_state(env) -> dict:
    """读回状态文件（验证标记持久化用）。"""
    return json.loads(env.state_path.read_text(encoding="utf-8"))


class FakeHttp:
    """记录请求并按预设返回的 HTTP 替身。"""

    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, payload: dict, timeout: float) -> dict:
        self.calls.append((url, payload))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeHttp4xxExpired:
    """模拟网关返回 HTTP 4xx、body 带明确错误码 expired 的场景。

    走客户端真实的 ``_http_post_json`` 路径（monkeypatch ``lic.urlopen``），
    覆盖「HTTPError → 从 body 提取 code → LicenseError(code="expired")」
    这条链路，而不是直接抛 LicenseError 的捷径。
    """

    def __init__(self, status: int = 403) -> None:
        self.status = status

    def __call__(self, request, timeout: float):
        body = json.dumps(
            {"error": {"code": "expired", "message": "授权已到期，请续费"}},
            ensure_ascii=False).encode("utf-8")
        raise HTTPError(request.full_url, self.status, "Forbidden", {},
                        io.BytesIO(body))


# ---------------------------------------------------------------------------
# 停用路径：服务器明确说 expired → 立即停用
# ---------------------------------------------------------------------------
def test_heartbeat_expired_stops_client(env, signer):
    """心跳返回 ok=false + code=expired → 状态变 expired（不再宽限放行）。"""
    write_doc(env, signer, make_payload())
    write_state(env, last_ok_at=lic.now().isoformat())
    client = env.build(http_post=FakeHttp({
        "ok": False,
        "error": {"code": "expired", "message": "授权已到期，请续费"}}))
    st = client.refresh()
    assert st.state == "expired"
    assert st.ok is False
    assert st.days_left == 0
    assert "过期" in st.message and "续费" in st.message
    # last_error 留痕（排障用）+ 停用标记落盘
    assert "授权已到期" in read_state(env).get("last_error", "")
    assert read_state(env).get("server_expired_at")


def test_http_4xx_expired_body_stops_client(env, signer, monkeypatch):
    """HTTP 4xx + body 带 {"error":{"code":"expired"}} → 同样停用。"""
    write_doc(env, signer, make_payload())
    write_state(env, last_ok_at=lic.now().isoformat())
    monkeypatch.setattr(lic, "urlopen", FakeHttp4xxExpired(status=403))
    client = env.build(http_post=None)     # 走真实 _http_post_json
    st = client.refresh()
    assert st.state == "expired" and st.ok is False
    assert read_state(env).get("server_expired_at")


def test_server_expired_beats_local_validity_and_grace(env, signer):
    """停用标记优先于本地 expires_at 与离线宽限（fail-closed 判定链）。"""
    write_doc(env, signer, make_payload())          # 本地证还有 100 天
    write_state(env, last_ok_at=lic.now().isoformat(),   # 且刚联网确认过
                server_expired_at="2026-11-20 10:00")
    st = env.client.status()
    assert st.state == "expired" and st.ok is False
    assert "授权服务器已判定" in st.message
    assert "2026-11-20 10:00" in st.message


# ---------------------------------------------------------------------------
# 回归保护：网络不可达仍然走宽限，绝不停用
# ---------------------------------------------------------------------------
def test_network_failure_keeps_grace(env, signer):
    """心跳网络失败（code=network）→ 仍按宽限放行，不落停用标记。"""
    write_doc(env, signer, make_payload())
    write_state(env, last_ok_at=lic.now().isoformat())
    client = env.build(http_post=FakeHttp(
        lic.LicenseError("连接授权服务器失败", code="network")))
    st = client.refresh()
    assert st.ok is True and st.state in ("active", "offline_grace")
    assert not read_state(env).get("server_expired_at")


def test_http_4xx_without_error_code_keeps_grace(env, signer, monkeypatch):
    """4xx 无明确错误码（网关 500/502 之类）→ 按网络问题宽限，不停用。"""
    write_doc(env, signer, make_payload())
    write_state(env, last_ok_at=lic.now().isoformat())

    class FakeHttp4xxPlain:
        @staticmethod
        def __call__(request, timeout: float):
            raise HTTPError(request.full_url, 502, "Bad Gateway", {},
                            io.BytesIO(b"<html>Bad Gateway</html>"))

    monkeypatch.setattr(lic, "urlopen", FakeHttp4xxPlain())
    client = env.build(http_post=None)
    st = client.refresh()
    assert st.ok is True and st.state in ("active", "offline_grace")
    assert not read_state(env).get("server_expired_at")


# ---------------------------------------------------------------------------
# 标记持久化：重算 / 重启（新实例读盘）后仍然停用
# ---------------------------------------------------------------------------
def test_marker_persists_across_restart(env, signer):
    """停用标记写盘后，重建客户端实例（模拟进程重启）仍是 expired。"""
    write_doc(env, signer, make_payload())
    write_state(env, last_ok_at=lic.now().isoformat())
    client = env.build(http_post=FakeHttp({
        "ok": False,
        "error": {"code": "expired", "message": "授权已到期，请续费"}}))
    client.refresh()
    assert client.status().state == "expired"
    # 新实例：状态文件重新从磁盘读回
    restarted = env.build()
    st = restarted.status()
    assert st.state == "expired" and st.ok is False
    assert restarted.status(touch=True).state == "expired"


# ---------------------------------------------------------------------------
# 恢复路径：续期后重新激活 / 心跳成功即清除标记
# ---------------------------------------------------------------------------
def test_renewal_via_refresh_with_new_license_clears_marker(env, signer):
    """续期后心跳成功并带回新许可证 → 标记清除、状态恢复 active。"""
    write_doc(env, signer, make_payload())
    write_state(env, last_ok_at=lic.now().isoformat())
    stop = env.build(http_post=FakeHttp({
        "ok": False,
        "error": {"code": "expired", "message": "授权已到期，请续费"}}))
    assert stop.refresh().state == "expired"

    renewed_doc = make_doc(signer, make_payload(
        license_id="LIC-TEST-0002",
        expires_at=(lic.now() + timedelta(days=365)).isoformat()))
    resumed = env.build(http_post=FakeHttp({"ok": True, "license": renewed_doc}))
    st = resumed.refresh()
    assert st.state == "active" and st.ok is True
    assert st.license_id == "LIC-TEST-0002"
    assert not read_state(env).get("server_expired_at")


def test_renewal_via_activate_clears_marker(env, signer):
    """续期后重新激活 → 标记清除、状态恢复 active。"""
    write_doc(env, signer, make_payload())
    write_state(env, last_ok_at=lic.now().isoformat(),
                server_expired_at="2026-11-20 10:00")
    renewed_doc = make_doc(signer, make_payload(
        expires_at=(lic.now() + timedelta(days=365)).isoformat()))
    client = env.build(http_post=FakeHttp({"ok": True, "license": renewed_doc}))
    st = client.activate("KF-RENEW-0001")
    assert st.state == "active" and st.ok is True
    assert not read_state(env).get("server_expired_at")


def test_ok_heartbeat_clears_marker(env, signer):
    """供应商在后台恢复记录后心跳直接 ok=true（未带新证）→ 标记同样清除。"""
    write_doc(env, signer, make_payload())
    write_state(env, last_ok_at=lic.now().isoformat(),
                server_expired_at="2026-11-20 10:00")
    client = env.build(http_post=FakeHttp({"ok": True}))
    st = client.refresh()
    assert st.ok is True and st.state == "active"
    assert not read_state(env).get("server_expired_at")


# ---------------------------------------------------------------------------
# 回归保护：revoked 路径行为不变（且优先于 expired 标记）
# ---------------------------------------------------------------------------
def test_revoked_path_unchanged(env, signer):
    """心跳返回 code=revoked → 落 revoked_at 锁死（修复前既有行为）。"""
    write_doc(env, signer, make_payload())
    write_state(env, last_ok_at=lic.now().isoformat())
    client = env.build(http_post=FakeHttp({
        "ok": False,
        "error": {"code": "revoked", "message": "授权已于 2026-11-20 被吊销"}}))
    st = client.refresh()
    assert st.state == "revoked" and st.ok is False
    assert "吊销" in st.message
    assert read_state(env).get("revoked_at")


def test_revoked_takes_priority_over_expired_marker(env, signer):
    """同时存在 revoked 与 server_expired 标记时按 revoked 提示（判定链顺序）。"""
    write_doc(env, signer, make_payload())
    write_state(env, last_ok_at=lic.now().isoformat(),
                revoked_at="2026-11-21 09:00",
                server_expired_at="2026-11-20 10:00")
    st = env.client.status()
    assert st.state == "revoked" and st.ok is False
    assert "吊销" in st.message
