# -*- coding: utf-8 -*-
"""授权 API 与拦截中间件测试。

覆盖：
- `/api/license/status|machine-code|activate|refresh` 的契约
- 未授权时业务接口 403、白名单（授权接口 / 状态 / 非 API 路径）放行
- 激活成功后自动拉起运行时（不需要重启程序）

全部离线：HTTP 用替身，不发真实请求。

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_license_api.py -v --basetemp=D:/tmp/pt-licapi
"""
from __future__ import annotations

import base64
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import license as lic
from app.api import ALL_ROUTERS
from app.api import deps
from app.api.license_guard import make_license_guard
from app.db import Database
from app.notifier import Notifier
from tests.test_api import FakePipeline, FakeSender, FakeWechatService

MACHINE = "AAAA-BBBB-CCCC-DDDD-EEEE-FFFF-1111-2222"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@pytest.fixture()
def signer(monkeypatch):
    """临时密钥对 + 注入公钥（不依赖仓库里的 dev 私钥）。"""
    private_key = Ed25519PrivateKey.generate()
    raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    monkeypatch.setitem(lic.PUBLIC_KEYS, "test",
                        base64.b64encode(raw).decode("ascii"))
    return private_key


def make_doc(private_key, *, days: int = 100,
             machine: str = MACHINE) -> dict:
    """签发一张可用的许可证文档。"""
    now = lic.now()
    payload = {
        "license_id": "LIC-API-0001",
        "machine_code": machine,
        "customer": "接口测试客户",
        "edition": "standard",
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(days=days)).isoformat(),
        "grace_days": 7,
        "features": ["monitor", "reply", "ai"],
    }
    signature = private_key.sign(lic.payload_bytes(payload))
    return {"key_id": "test", "payload": payload,
            "signature": base64.b64encode(signature).decode("ascii")}


class HttpStub:
    """HTTP 替身：按 URL 路径返回预设响应（或抛预设异常）。"""

    def __init__(self, response=None, exc: Exception | None = None) -> None:
        self.response = response
        self.exc = exc
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, payload: dict, timeout: float) -> dict:
        self.calls.append((url, payload))
        if self.exc is not None:
            raise self.exc
        return self.response


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """真实路由 + 替身依赖 + 临时库 + 启用的授权（临时文件路径）。"""
    monkeypatch.setenv("KEFU_LICENSE_ENABLED", "1")
    monkeypatch.setenv("KEFU_LICENSE_SERVER", "http://127.0.0.1:1")
    monkeypatch.delenv("KEFU_LICENSE_MODE", raising=False)
    db = Database(str(tmp_path / "license_api.sqlite3"))
    deps.reset_for_tests()
    deps.configure(db=db, wechat=FakeWechatService(), pipeline=FakePipeline(),
                   sender=FakeSender(), notifier=Notifier())
    runtime_calls = {"count": 0}

    def start_runtime() -> None:
        runtime_calls["count"] += 1

    deps.configure_runtime(start_runtime)

    def build_client(http=None, machine: str = MACHINE) -> lic.LicenseClient:
        return lic.LicenseClient(
            db=db, http_post=http,
            machine_code_provider=lambda: machine,
            license_path=tmp_path / "license.json",
            state_path=tmp_path / "license.state.json")

    app = FastAPI()
    app.middleware("http")(make_license_guard(
        lambda: deps.get_license_client().status(), cache_sec=0))
    for router in ALL_ROUTERS:
        app.include_router(router)
    yield SimpleNamespace(db=db, app=app, client=TestClient(app),
                          build_client=build_client, http=http_stub_holder(),
                          runtime_calls=runtime_calls,
                          license_path=tmp_path / "license.json")
    deps.reset_for_tests()
    db.close()


def http_stub_holder() -> dict:
    """可在用例中替换的 HTTP 替身容器（注入后由用例设置）。"""
    return {"stub": HttpStub(None)}


def use_http(env, stub) -> lic.LicenseClient:
    """把替身 HTTP 注入 deps，供路由内部创建的客户端使用。"""
    client = env.build_client(http=stub)
    deps.configure(db=env.db, wechat=FakeWechatService(),
                   pipeline=FakePipeline(), sender=FakeSender(),
                   notifier=Notifier(), license_client=client)
    return client


# ---------------------------------------------------------------------------
# 状态接口
# ---------------------------------------------------------------------------
def test_status_zero_in_db_no_longer_disables(env, monkeypatch):
    """settings 表 license.enabled=0 **不再**等于关闭授权（v2 fail-closed）。

    现场最容易被顺手改的就是这行；改完仍然要求激活，客户没法靠改库白用。
    """
    monkeypatch.delenv("KEFU_LICENSE_ENABLED", raising=False)
    env.db.set_setting("license.enabled", "0")
    deps.configure(db=env.db, wechat=FakeWechatService(),
                   pipeline=FakePipeline(), sender=FakeSender(),
                   notifier=Notifier(),
                   license_client=env.build_client())
    data = env.client.get("/api/license/status").json()
    assert data["enabled"] is True and data["state"] == "unlicensed"
    assert data["dev_mode"] is False
    assert data["enabled_source"] == "db"


def test_status_dev_mode_via_db_token(env, monkeypatch):
    """库里写入口令指纹 → 测试模式（enabled=false + dev_mode=true）。"""
    monkeypatch.delenv("KEFU_LICENSE_ENABLED", raising=False)
    env.db.set_setting("license.enabled", lic.DEV_TOKEN_SHA256)
    deps.configure(db=env.db, wechat=FakeWechatService(),
                   pipeline=FakePipeline(), sender=FakeSender(),
                   notifier=Notifier(),
                   license_client=env.build_client())
    data = env.client.get("/api/license/status").json()
    assert data["enabled"] is False and data["ok"] is True
    assert data["state"] == "disabled" and data["dev_mode"] is True
    assert "测试模式" in data["message"]


def test_enabled_source_env(env):
    """开关来自环境变量时标注 env（优先级高于数据库）。"""
    use_http(env, HttpStub(None))
    data = env.client.get("/api/license/status").json()
    assert data["enabled"] is True
    assert data["enabled_source"] == "env"


def test_status_unlicensed(env):
    """启用授权但没激活 → unlicensed + ok=false（前端弹激活框）。"""
    use_http(env, HttpStub(None))
    data = env.client.get("/api/license/status").json()
    assert data["enabled"] is True and data["state"] == "unlicensed"
    assert data["ok"] is False
    assert "激活" in data["message"]


def test_machine_code_endpoint(env):
    """机器码接口返回本机机器码与成分列表。"""
    use_http(env, HttpStub(None))
    data = env.client.get("/api/license/machine-code").json()
    assert data["ok"] is True
    assert data["machine_code"] == MACHINE
    assert isinstance(data["components"], list)


# ---------------------------------------------------------------------------
# 激活 / 导入 / 刷新
# ---------------------------------------------------------------------------
def test_activate_success_and_starts_runtime(env, signer):
    """激活成功：返回 active、落盘许可证、自动拉起运行时（不必重启）。"""
    stub = HttpStub({"ok": True, "license": make_doc(signer)})
    use_http(env, stub)
    resp = env.client.post("/api/license/activate",
                           json={"activation_code": "KF-TEST-0001"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "active" and data["ok"] is True
    assert data["customer"] == "接口测试客户"
    assert env.license_path.exists()
    assert stub.calls[0][0].endswith("/api/v1/license/activate")
    assert stub.calls[0][1]["activation_code"] == "KF-TEST-0001"
    assert env.runtime_calls["count"] == 1


def test_activate_failure_returns_chinese(env):
    """激活失败：400 + 中文原因 + 可操作提示。"""
    use_http(env, HttpStub(exc=lic.LicenseError("激活码不存在",
                                               code="invalid_code")))
    resp = env.client.post("/api/license/activate",
                           json={"activation_code": "KF-BAD"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "激活码不存在" in detail
    assert "核对" in detail            # 错误码被翻译成建议
    assert env.runtime_calls["count"] == 0


def test_activate_rejects_foreign_license(env, signer):
    """服务器给别的机器码签的证 → 拒绝并提示机器码不一致。"""
    use_http(env, HttpStub({"ok": True, "license": make_doc(
        signer, machine="9999-8888-7777-6666-5555-4444-3333-2222")}))
    resp = env.client.post("/api/license/activate",
                           json={"activation_code": "K"})
    assert resp.status_code == 400
    assert "机器码" in resp.json()["detail"]


def test_import_endpoint_removed(env, signer):
    """离线导入已下线：接口不存在（客户机必须联网，不需要线下发证）。"""
    use_http(env, HttpStub(None))
    routes = [getattr(r, "path", "") for r in env.app.routes]
    assert "/api/license/import" not in routes
    resp = env.client.post("/api/license/import",
                           json={"license": make_doc(signer)})
    assert resp.status_code == 404


def test_refresh_network_error_keeps_license(env, signer):
    """联网确认失败不影响已激活状态（只推迟下次确认）。"""
    use_http(env, HttpStub(None))
    env.license_path.write_text(
        json.dumps(make_doc(signer), ensure_ascii=False), encoding="utf-8")
    use_http(env, HttpStub(exc=lic.LicenseError("连不上", code="network")))
    data = env.client.post("/api/license/refresh").json()
    assert data["ok"] is True and data["state"] == "active"


# ---------------------------------------------------------------------------
# 拦截中间件
# ---------------------------------------------------------------------------
def test_guard_blocks_business_api_when_unlicensed(env):
    """未授权：业务接口 403（中文原因 + 状态码），数据不受影响。"""
    use_http(env, HttpStub(None))
    resp = env.client.get("/api/opportunity-types")
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "license_required"
    assert body["license_state"] == "unlicensed"
    assert "数据未受影响" in body["detail"]


def test_guard_allows_license_and_status(env):
    """白名单：授权接口与 /api/status 在未授权时仍可访问。"""
    use_http(env, HttpStub(None))
    assert env.client.get("/api/license/status").status_code == 200
    assert env.client.get("/api/license/machine-code").status_code == 200
    # /api/status 依赖替身字段，状态码不是 403 即视为放行
    assert env.client.get("/api/status").status_code != 403


def test_guard_allows_non_api_paths(env):
    """非 /api 路径（静态资源、SPA）不拦。"""
    use_http(env, HttpStub(None))
    assert env.client.get("/").status_code != 403
    assert env.client.get("/favicon.ico").status_code != 403


def test_guard_passes_after_activation(env, signer):
    """激活后业务接口恢复（无需重启）。"""
    use_http(env, HttpStub(None))
    assert env.client.get("/api/opportunity-types").status_code == 403
    env.license_path.write_text(
        json.dumps(make_doc(signer), ensure_ascii=False), encoding="utf-8")
    assert env.client.get("/api/opportunity-types").status_code == 200


def test_guard_blocks_when_expired(env, signer):
    """过期许可证：业务接口同样被拦，且状态为 expired。"""
    use_http(env, HttpStub(None))
    env.license_path.write_text(
        json.dumps(make_doc(signer, days=-1), ensure_ascii=False),
        encoding="utf-8")
    assert env.client.get("/api/opportunity-types").status_code == 403
    assert env.client.get("/api/license/status").json()["state"] == "expired"


def test_guard_fail_open_on_internal_error(env):
    """授权模块自身异常时放行（宁可少赚一单也不让客户开不了工）。"""
    class Boom:
        def status(self, touch: bool = False):
            raise RuntimeError("授权模块炸了")

    deps.configure(db=env.db, wechat=FakeWechatService(),
                   pipeline=FakePipeline(), sender=FakeSender(),
                   notifier=Notifier(), license_client=Boom())
    assert env.client.get("/api/opportunity-types").status_code == 200
