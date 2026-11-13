# -*- coding: utf-8 -*-
"""授权核心逻辑测试：机器码、签名验签、状态机、离线宽限、时钟回拨。

全部离线：不联网、不读真实 data/ 目录、不依赖 Windows 硬件（机器码用注入
的固定值）。

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_license_core.py -v --basetemp=D:/tmp/pt-lic
"""
from __future__ import annotations

import base64
import json
import time
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app import license as lic

MACHINE = "AAAA-BBBB-CCCC-DDDD-EEEE-FFFF-1111-2222"


# ---------------------------------------------------------------------------
# 工具
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
    """写本地状态文件（模拟历史联网记录 / 吊销标记 / 时钟基准）。"""
    env.state_path.write_text(json.dumps(fields, ensure_ascii=False),
                              encoding="utf-8")


# ---------------------------------------------------------------------------
# 机器码
# ---------------------------------------------------------------------------
def test_machine_code_format():
    """机器码是 32 位大写十六进制、4 位一组的 8 段。"""
    code = lic.machine_code(force=True)
    parts = code.split("-")
    assert len(parts) == 8
    assert all(len(p) == 4 and all(c in "0123456789ABCDEF" for c in p)
               for p in parts)


def test_machine_code_stable():
    """同一次进程内重复取机器码必须完全一致（授权不能被随机化）。"""
    first = lic.machine_code(force=True)
    assert lic.machine_code(force=True) == first
    assert lic.machine_code() == first


def test_collect_fingerprint_no_random():
    """指纹成分不含时间/随机量（同一台机器每次一样）。"""
    a = lic.collect_fingerprint()
    b = lic.collect_fingerprint()
    assert a == b
    # 只允许这三个键（WMI 的主板 UUID 刻意不参与）
    assert set(a) <= {"machine_guid", "volume_serial", "host"}


def test_clean_hw_filters_placeholders():
    """厂商占位串必须被过滤（否则一批机器算出同一个码）。"""
    for junk in ("", "  ", "0", "Default string", "To be filled by O.E.M.",
                 "None", "System Serial Number"):
        assert lic._clean_hw(junk) == ""
    assert lic._clean_hw(" ab-cd  ef ") == "AB-CD EF"


# ---------------------------------------------------------------------------
# 签名
# ---------------------------------------------------------------------------
def test_payload_bytes_canonical():
    """签名原文必须键排序、紧凑、不转义中文。"""
    raw = lic.payload_bytes({"b": 1, "a": "中文"})
    assert raw == '{"a":"中文","b":1}'.encode("utf-8")


def test_verify_ok(signer):
    """正常签名可以通过验签。"""
    doc = make_doc(signer, make_payload())
    assert lic.verify_license(doc) == (True, "")


def test_verify_rejects_tampered(signer):
    """改 payload 任何一个字段都必须验签失败。"""
    doc = make_doc(signer, make_payload())
    doc["payload"]["expires_at"] = "2099-01-01T00:00:00+08:00"
    ok, reason = lic.verify_license(doc)
    assert not ok and "签名" in reason


def test_verify_rejects_unknown_key_id(signer):
    """未知 key_id（客户端版本过旧）给出明确原因。"""
    doc = make_doc(signer, make_payload())
    doc["key_id"] = "nope"
    ok, reason = lic.verify_license(doc)
    assert not ok and "密钥标识未知" in reason


def test_verify_rejects_missing_parts(signer):
    """缺 payload / signature / 非 dict 都拒绝。"""
    assert lic.verify_license(None)[0] is False
    assert lic.verify_license({})[0] is False
    doc = make_doc(signer, make_payload())
    assert lic.verify_license({"payload": doc["payload"],
                               "key_id": "test"})[0] is False


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------
def test_dev_token_disables(monkeypatch, tmp_path):
    """只有厂商测试口令才能关闭授权校验（口令指纹形式也认）。"""
    monkeypatch.setenv("KEFU_LICENSE_ENABLED", lic.DEV_TOKEN_SHA256)
    client = lic.LicenseClient(machine_code_provider=lambda: MACHINE,
                               license_path=tmp_path / "l.json",
                               state_path=tmp_path / "s.json")
    st = client.status()
    assert st.state == "disabled" and st.ok is True
    assert st.enabled is False and st.dev_mode is True
    assert "测试模式" in st.message


def test_switch_off_values_no_longer_disable(monkeypatch, tmp_path):
    """v2 fail-closed：0/false/off/空 都不再等于「关闭授权」。

    这是本次加固的核心——客户改数据库或配置文件把开关写成 0，仍然要求授权。
    """
    for value in ("0", "false", "off", "no", "  ", "随便写"):
        monkeypatch.setenv("KEFU_LICENSE_ENABLED", value)
        client = lic.LicenseClient(machine_code_provider=lambda: MACHINE,
                                   license_path=tmp_path / "l.json",
                                   state_path=tmp_path / "s.json")
        st = client.status()
        assert st.enabled is True, value
        assert st.dev_mode is False, value
        assert st.state == "unlicensed", value


def test_plaintext_token_matches(monkeypatch):
    """口令明文与指纹等价（不依赖真实口令，测试里临时换一个）。"""
    monkeypatch.setattr(lic, "DEV_TOKEN_SHA256", lic.token_digest("tmp-token"))
    assert lic.is_dev_token("tmp-token") is True
    assert lic.is_dev_token(lic.token_digest("tmp-token")) is True
    assert lic.is_dev_token("tmp-token ") is True      # 容忍首尾空白
    assert lic.is_dev_token("tmp") is False
    assert lic.is_dev_token("") is False
    assert lic.is_dev_token(None) is False


def test_force_env_beats_dev_token(monkeypatch, tmp_path):
    """环境变量强制授权优先于测试口令（交付脚本用这条兜底）。"""
    monkeypatch.setenv("KEFU_LICENSE_ENABLED", lic.DEV_TOKEN_SHA256)
    monkeypatch.setenv("KEFU_LICENSE_MODE", "enforce")
    client = lic.LicenseClient(machine_code_provider=lambda: MACHINE,
                               license_path=tmp_path / "l.json",
                               state_path=tmp_path / "s.json")
    st = client.status()
    assert st.enabled is True and st.dev_mode is False
    assert st.state == "unlicensed"
    assert st.enabled_source == "env-force"


def test_unlicensed_when_no_file(env):
    """没有许可证文件 → unlicensed（要求激活）。"""
    st = env.client.status()
    assert st.state == "unlicensed" and st.ok is False
    assert st.machine_code == MACHINE
    assert "激活" in st.message


def test_active_with_valid_license(env, signer):
    """有效许可证 + 刚联网确认过 → active。"""
    write_doc(env, signer, make_payload())
    write_state(env, last_ok_at=lic.now().isoformat())
    st = env.client.status()
    assert st.state == "active" and st.ok is True
    assert st.license_id == "LIC-TEST-0001"
    assert st.customer == "测试客户"
    assert st.days_left is not None and 95 <= st.days_left <= 100


def test_last_error_surfaces_in_status(env, signer):
    """联网确认失败的原因要带到 status 里（否则界面只能显示"授权正常"）。

    现场案例：服务端记录被删/吊销后，客户端在宽限期内仍判 active，
    用户完全看不出两边不一致。
    """
    write_doc(env, signer, make_payload())
    write_state(env, last_ok_at=lic.now().isoformat(),
                last_error="服务器上没有这台电脑的授权记录")
    st = env.client.status()
    assert st.ok is True and st.state == "active"
    assert st.last_error == "服务器上没有这台电脑的授权记录"


def test_expired(env, signer):
    """超过 expires_at → expired（即使签名有效）。"""
    write_doc(env, signer, make_payload(
        expires_at=(lic.now() - timedelta(days=1)).isoformat()))
    write_state(env, last_ok_at=lic.now().isoformat())
    st = env.client.status()
    assert st.state == "expired" and st.ok is False and st.days_left == 0


def test_machine_mismatch(env, signer):
    """许可证绑定的是别的机器码 → invalid，并提示重新申请。"""
    write_doc(env, signer, make_payload(
        machine_code="9999-8888-7777-6666-5555-4444-3333-2222"))
    write_state(env, last_ok_at=lic.now().isoformat())
    st = env.client.status()
    assert st.state == "invalid" and st.ok is False
    assert "机器码" in st.message


def test_tampered_file_is_invalid(env, signer):
    """客户手改 license.json 的到期日 → 验签失败 → invalid。"""
    write_doc(env, signer, make_payload())
    doc = json.loads(env.license_path.read_text(encoding="utf-8"))
    doc["payload"]["expires_at"] = "2099-01-01T00:00:00+08:00"
    env.license_path.write_text(json.dumps(doc), encoding="utf-8")
    assert env.client.status().state == "invalid"


def test_offline_grace_still_ok(env, signer):
    """3 天没联网（宽限 7 天）→ offline_grace，仍可用并提示离线。"""
    write_doc(env, signer, make_payload())
    write_state(env, last_ok_at=(lic.now() - timedelta(days=3)).isoformat())
    st = env.client.status()
    assert st.state == "offline_grace" and st.ok is True and st.offline is True


def test_offline_expired_locks(env, signer):
    """超过宽限期没联网 → offline_expired，锁死并提示联网。"""
    write_doc(env, signer, make_payload())
    write_state(env, last_ok_at=(lic.now() - timedelta(days=10)).isoformat())
    st = env.client.status()
    assert st.state == "offline_expired" and st.ok is False
    assert "联网" in st.message


def test_grace_days_from_payload(env, signer):
    """宽限天数以许可证 payload 为准（服务器可下发更短/更长）。"""
    write_doc(env, signer, make_payload(grace_days=30))
    write_state(env, last_ok_at=(lic.now() - timedelta(days=20)).isoformat())
    st = env.client.status()
    assert st.grace_days == 30
    assert st.state == "offline_grace" and st.ok is True


def test_revoked_flag_locks_even_if_valid(env, signer):
    """服务器吊销过（本地记了 revoked_at）→ 一律锁死。"""
    write_doc(env, signer, make_payload())
    write_state(env, last_ok_at=lic.now().isoformat(),
                revoked_at="2026-11-20 10:00")
    st = env.client.status()
    assert st.state == "revoked" and st.ok is False
    assert "吊销" in st.message


def test_clock_rollback_detected(env, signer):
    """系统时间比历史最大时间早 1 天以上 → clock_rollback。"""
    write_doc(env, signer, make_payload())
    write_state(env, last_ok_at=lic.now().isoformat(),
                max_seen_ts=time.time() + 3 * 86400)
    st = env.client.status()
    assert st.state == "clock_rollback" and st.ok is False


def test_missing_expires_at_invalid(env, signer):
    """payload 缺 expires_at → invalid（不给"永久有效"的空子）。"""
    payload = make_payload()
    payload.pop("expires_at")
    write_doc(env, signer, payload)
    write_state(env, last_ok_at=lic.now().isoformat())
    assert env.client.status().state == "invalid"


def test_touch_updates_max_seen(env, signer):
    """touch=True 时把 max_seen_ts 落盘（防回拨的时间基准）。"""
    write_doc(env, signer, make_payload())
    write_state(env, last_ok_at=lic.now().isoformat())
    env.client.status(touch=True)
    state = json.loads(env.state_path.read_text(encoding="utf-8"))
    assert state["max_seen_ts"] > time.time() - 60


# ---------------------------------------------------------------------------
# 联网能力（HTTP 用替身，不真发请求）
# ---------------------------------------------------------------------------
class FakeHttp:
    """记录请求并按预设返回的 HTTP 替身。"""

    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, payload: dict, timeout: float) -> dict:
        self.calls.append((url, payload))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_activate_signs_in_and_writes_file(env, signer):
    """激活成功：落盘许可证 + 记录 last_ok_at + 状态变 active。"""
    doc = make_doc(signer, make_payload())
    http = FakeHttp({"ok": True, "license": doc})
    client = env.build(http_post=http)
    st = client.activate("KF-TEST-0001")
    assert st.state == "active" and st.ok is True
    assert env.license_path.exists()
    assert http.calls[0][1]["machine_code"] == MACHINE
    assert http.calls[0][1]["activation_code"] == "KF-TEST-0001"
    state = json.loads(env.state_path.read_text(encoding="utf-8"))
    assert state["last_ok_at"]


def test_activate_rejects_foreign_machine(env, signer):
    """服务器签回来的证绑了别的机器码 → 客户端拒绝落盘。"""
    doc = make_doc(signer, make_payload(
        machine_code="9999-8888-7777-6666-5555-4444-3333-2222"))
    client = env.build(http_post=FakeHttp({"ok": True, "license": doc}))
    with pytest.raises(lic.LicenseError) as exc:
        client.activate("KF-TEST")
    assert exc.value.code == "machine_mismatch"
    assert not env.license_path.exists()


def test_activate_error_message_kept(env):
    """服务端错误码/中文原因原样抛给上层（前端直接展示）。"""
    client = env.build(http_post=FakeHttp({
        "error": {"code": "invalid_code", "message": "激活码不存在"}}))
    with pytest.raises(lic.LicenseError) as exc:
        client.activate("KF-BAD")
    assert exc.value.code == "invalid_code"
    assert "激活码不存在" in str(exc.value)


def test_refresh_keeps_grace_on_network_error(env, signer):
    """心跳网络失败不改变授权（只是不刷新 last_ok_at）。"""
    write_doc(env, signer, make_payload())
    write_state(env, last_ok_at=lic.now().isoformat())
    client = env.build(http_post=FakeHttp(
        lic.LicenseError("连不上", code="network")))
    st = client.refresh()
    assert st.ok is True and st.state == "active"


def test_refresh_marks_revoked(env, signer):
    """服务器说已吊销 → 本地标记，之后锁死。"""
    write_doc(env, signer, make_payload())
    write_state(env, last_ok_at=lic.now().isoformat())
    client = env.build(http_post=FakeHttp({
        "ok": False, "error": {"code": "revoked", "message": "已吊销"}}))
    st = client.refresh()
    assert st.state == "revoked" and st.ok is False


def test_needs_refresh_by_interval(env, signer):
    """距上次确认超过配置间隔才需要刷新。"""
    write_doc(env, signer, make_payload())
    write_state(env, last_ok_at=lic.now().isoformat())
    assert env.build().needs_refresh() is False
    write_state(env, last_ok_at=(lic.now() - timedelta(days=2)).isoformat())
    assert env.build().needs_refresh() is True


def test_extract_error_supports_both_shapes():
    """错误体两种包装都能解析（顶层 error / FastAPI 的 detail.error）。"""
    assert lic.extract_error({"error": {"code": "a", "message": "甲"}}) \
        == ("a", "甲")
    assert lic.extract_error(
        {"detail": {"error": {"code": "b", "message": "乙"}}}) == ("b", "乙")
    assert lic.extract_error({"detail": "只是一句话"}) == ("", "只是一句话")
    assert lic.extract_error(None) == ("", "")
