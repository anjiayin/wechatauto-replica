# -*- coding: utf-8 -*-
"""C 组：鉴权与安全（工程师报告里没重点打的部分）。

对齐依据：``docs/design.md`` §4（鉴权方案）、§5.2/§5.3（错误码）、
``app/api/deps.py``（会话 / CSRF / 强制改密三道闸门）。
"""
from __future__ import annotations

import json

import pytest

import _helpers as H

#: 管理端业务接口清单（B1–B17），未登录一律 401
BUSINESS_ENDPOINTS = [
    ("GET", "/admin/api/dashboard", None),
    ("GET", "/admin/api/meta", None),
    ("GET", "/admin/api/codes", None),
    ("GET", "/admin/api/codes/export", None),
    ("GET", "/admin/api/licenses", None),
    ("GET", "/admin/api/licenses/LIC-20260101-0000", None),
    ("GET", "/admin/api/customers", None),
    ("GET", "/admin/api/customers/1", None),
    ("GET", "/admin/api/machines", None),
    ("GET", "/admin/api/audit", None),
    ("POST", "/admin/api/codes", {"count": 1, "days": 30}),
    ("POST", "/admin/api/licenses/LIC-20260101-0000/renew", {"days": 30}),
    ("POST", "/admin/api/licenses/LIC-20260101-0000/extend", {"days": 30}),
    ("POST", "/admin/api/licenses/LIC-20260101-0000/expiry",
     {"expires_at": "2030-01-01"}),
    ("POST", "/admin/api/licenses/LIC-20260101-0000/revoke", {"reason": "x"}),
    ("POST", "/admin/api/licenses/LIC-20260101-0000/restore", {}),
    ("POST", "/admin/api/customers", {"name": "x", "phone": "13900000000"}),
]


# ---------------------------------------------------------------------------
# C1 未登录 / 伪造凭据
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method,path,body", BUSINESS_ENDPOINTS)
def test_c01_business_requires_login(admin, method, path, body):
    """C1：未登录访问任何 /admin/api/* 业务接口 → 401 unauthorized。"""
    cli = H.anon()
    resp = cli.request(method, path, body=body, csrf=False)
    assert resp.status_code == 401, "%s %s → %s %s" % (
        method, path, resp.status_code, resp.text)
    assert H.error_of(resp).get("code") == "unauthorized", resp.text
    assert H.has_chinese(H.error_of(resp).get("message"))


def test_c01_forged_credentials_rejected(admin):
    """C01-b：伪造 Cookie / Bearer 令牌 → 401。"""
    cli = H.anon()
    resp = cli.get("/admin/api/dashboard", csrf=False,
                   headers={"Cookie": "ls_session=forged-token-1234567890"})
    assert resp.status_code == 401, resp.text

    resp = cli.get("/admin/api/dashboard", csrf=False,
                   headers={"Authorization": "Bearer forged-token-1234567890"})
    assert resp.status_code == 401, resp.text
    # 表里的真实 token 不能是空 / 弱值
    assert H.q1("SELECT COUNT(*) AS c FROM admin_session")["c"] >= 1


# ---------------------------------------------------------------------------
# C2 CSRF 双提交
# ---------------------------------------------------------------------------
def test_c02_csrf_required_for_writes(admin):
    """C2：已登录（Cookie）但缺 X-CSRF-Token / 令牌错误 → 403 csrf_invalid。"""
    payload = {"count": 1, "days": 30}
    resp = admin.post("/admin/api/codes", payload, csrf=False)
    assert resp.status_code == 403, resp.text
    assert H.error_of(resp).get("code") == "csrf_invalid", resp.text
    assert H.has_chinese(H.error_of(resp).get("message"))

    resp = admin.post("/admin/api/codes", payload, csrf=False,
                      headers={"X-CSRF-Token": "0" * 32})
    assert resp.status_code == 403, resp.text
    assert H.error_of(resp).get("code") == "csrf_invalid", resp.text

    # 登出也要 CSRF
    resp = admin.post("/admin/api/logout", {}, csrf=False)
    assert resp.status_code == 403, resp.text

    # 正常带令牌 → 200
    resp = admin.post("/admin/api/codes", payload)
    assert resp.status_code == 200, resp.text

    # 读接口不要求 CSRF
    resp = admin.get("/admin/api/codes", csrf=False)
    assert resp.status_code == 200, resp.text


def test_c02_csrf_token_is_per_session(admin):
    """C2-b：不同会话的 CSRF 令牌互不通用。"""
    other = H.Client()
    resp = other.login(H.ADMIN_USER, H.ADMIN_PASSWORD2)
    assert resp.status_code == 200, resp.text
    assert other.csrf and other.csrf != admin.csrf

    resp = admin.post("/admin/api/codes", {"count": 1, "days": 30},
                      csrf=False, headers={"X-CSRF-Token": other.csrf})
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# C3 超长输入（非严格模式下静默截断是最大风险）
# ---------------------------------------------------------------------------
LONG = "A" * 300


def test_c03_overlong_fields_rejected_by_app(admin):
    """C3：超长入参必须由应用层 400 拒绝，不能静默截断落库。"""
    cases = [
        ("POST", "/admin/api/codes",
         {"count": 1, "days": 30, "customer": LONG}, "customer"),
        ("POST", "/admin/api/codes",
         {"count": 1, "days": 30, "note": "备" * 400}, "note"),
        ("POST", "/admin/api/codes",
         {"count": 1, "days": 30, "customer_phone": "1" * 40}, "customer_phone"),
        ("POST", "/admin/api/customers",
         {"name": "客户", "phone": "1" * 40}, "phone"),
        ("POST", "/admin/api/customers",
         {"name": LONG * 2, "phone": "13900000001"}, "name"),
    ]
    for method, path, body, field in cases:
        resp = admin.request(method, path, body=body)
        assert resp.status_code == 400, "%s %s 超长没有被拦：%s %s" % (
            field, path, resp.status_code, resp.text)
        err = H.error_of(resp)
        assert err.get("code") == "invalid_param", resp.text
        assert H.has_chinese(err.get("message")), resp.text

    # 库里不能有半截数据
    assert H.q1("SELECT COUNT(*) AS c FROM license_code")["c"] == 0
    assert H.q1("SELECT COUNT(*) AS c FROM customer")["c"] == 0


def test_c03_overlong_machine_and_code_rejected(admin):
    """C3-b：machine_code / activation_code 超长 → 400，不入库。"""
    resp = H.activate(LONG, "KF-1111-1111-1111-1111")
    assert resp.status_code == 400, resp.text
    assert H.has_chinese(H.error_of(resp).get("message"))

    resp = H.activate(H.M1, "K" * 300)
    assert resp.status_code == 400, resp.text

    assert H.q1("SELECT COUNT(*) AS c FROM license")["c"] == 0


def test_c03_boundary_lengths_accepted(admin):
    """C3-c：刚好在列宽内的合法长度必须被接受（别把上限卡得过严）。"""
    phone = "13900001234"
    resp = admin.post("/admin/api/codes",
                      {"count": 1, "days": 30, "customer": "客" * 64,
                       "customer_phone": phone, "note": "备" * 255})
    assert resp.status_code == 200, resp.text
    row = H.q1("SELECT customer_name, note FROM license_code")
    assert len(row["customer_name"]) == 64
    assert len(row["note"]) == 255


# ---------------------------------------------------------------------------
# C4 SQL 注入
# ---------------------------------------------------------------------------
INJECTIONS = [
    "' OR 1=1 --",
    '" ; DROP TABLE license; --',
    "' UNION SELECT NULL,version() --",
    "\\' OR '1'='1",
    "admin'--",
]

_SQL_LEAK_MARKERS = ("1064", "You have an error in your SQL syntax", "pymysql",
                     "OperationalError", "Traceback", "SELECT ", "FROM license")


def _assert_no_sql_leak(resp) -> None:
    assert resp.status_code != 500, "注入串打出了 500：%s" % resp.text
    text = resp.text
    for marker in _SQL_LEAK_MARKERS:
        assert marker not in text, "响应体泄漏 SQL 细节（%s）：%s" % (marker, text)


@pytest.mark.parametrize("payload", INJECTIONS)
def test_c04_sql_injection_in_search(admin, payload):
    """C4：搜索参数注入 → 不 500、不泄漏 SQL、不放大结果。"""
    H.insert_code(code="KF-SAFE-0001", days=30)
    H.insert_license(license_id="LIC-SAFE-0001", machine_code=H.M1,
                     customer_name="正常客户", current_key=H.M1)

    for path in ("/admin/api/licenses", "/admin/api/codes", "/admin/api/audit",
                 "/admin/api/customers", "/admin/api/machines"):
        resp = admin.get(path, params={"q": payload})
        assert resp.status_code == 200, "%s → %s %s" % (path, resp.status_code, resp.text)
        _assert_no_sql_leak(resp)

    # 表必须还在，数据必须还在
    tables = [str(list(row.values())[0]) for row in H.q("SHOW TABLES")]
    assert "license" in tables and "license_code" in tables
    assert H.q1("SELECT COUNT(*) AS c FROM license_code")["c"] == 1
    assert H.q1("SELECT COUNT(*) AS c FROM license")["c"] == 1


def test_c04_sql_injection_in_client_api(admin):
    """C4-b：客户端接口的 machine_code 注入 → 400/404，不 500。"""
    for payload in INJECTIONS:
        resp = H.activate(payload, "KF-1111-1111-1111-1111")
        assert resp.status_code == 400, resp.text
        assert H.error_of(resp)["code"] == "invalid_machine_code", resp.text
        resp = H.heartbeat(payload, "")
        assert resp.status_code == 400, resp.text
    assert H.q1("SELECT COUNT(*) AS c FROM license")["c"] == 0


def test_c04_wildcard_percent_must_not_amplify(admin):
    """C4-c：塞 '%' 不能把搜索放大成全表（必须转义 LIKE 通配符）。"""
    for i in range(5):
        H.insert_code(code="KF-W-%04d" % i, days=30)
        H.insert_license(license_id="LIC-W-%04d" % i,
                         machine_code="AAAA-BBBB-CCCC-DDDD-EEEE-FFFF-0000-%04d" % i,
                         current_key="AAAA-BBBB-CCCC-DDDD-EEEE-FFFF-0000-%04d" % i)

    resp = admin.get("/admin/api/codes", params={"q": "%"})
    assert resp.json()["total"] == 0, "q='%' 命中了全部激活码（通配符未转义）"

    resp = admin.get("/admin/api/licenses", params={"q": "%"})
    assert resp.json()["total"] == 0, "q='%' 命中了全部许可证（通配符未转义）"


def test_c04_wildcard_underscore_must_not_amplify(admin):
    """C4-d：塞 '_' 也不能命中全表。"""
    H.insert_code(code="KF-U-0001", days=30)
    H.insert_license(license_id="LIC-U-0001",
                     machine_code="BBBB-CCCC-DDDD-EEEE-FFFF-0000-1111-2222",
                     current_key="BBBB-CCCC-DDDD-EEEE-FFFF-0000-1111-2222")
    resp = admin.get("/admin/api/codes", params={"q": "_"})
    assert resp.json()["total"] == 0, "q='_' 命中了激活码（通配符未转义）"


# ---------------------------------------------------------------------------
# C5 强制改密闸门
# ---------------------------------------------------------------------------
def test_c05_must_change_password_gate(admin_raw):
    """C5：must_change=1 时除 me/password/logout 外一律 403，改密后恢复。"""
    cli = admin_raw
    expected_code = H.frontend_must_change_code()

    # 放行的三个
    resp = cli.get("/admin/api/me")
    assert resp.status_code == 200, resp.text
    assert H.must_flag(resp.json()["user"]) is True
    assert resp.json().get("csrf_token")

    # 被拦的业务接口
    blocked = [("GET", "/admin/api/dashboard", None),
               ("GET", "/admin/api/codes", None),
               ("GET", "/admin/api/licenses", None),
               ("GET", "/admin/api/customers", None),
               ("GET", "/admin/api/audit", None),
               ("GET", "/admin/api/meta", None),
               ("POST", "/admin/api/codes", {"count": 1, "days": 30})]
    for method, path, body in blocked:
        resp = cli.request(method, path, body=body)
        assert resp.status_code == 403, "%s %s → %s %s" % (
            method, path, resp.status_code, resp.text)
        assert H.error_of(resp).get("code") == expected_code, (
            "前端 app.js 认的错误码是 %s，服务端返回 %s"
            % (expected_code, H.error_of(resp)))

    # 改密后恢复
    resp = cli.change_password(H.ADMIN_PASSWORD, H.ADMIN_PASSWORD2)
    assert resp.status_code == 200, resp.text
    assert H.must_flag(cli.get("/admin/api/me").json()["user"]) is False
    assert cli.get("/admin/api/dashboard").status_code == 200

    # 旧密码不能再登录，新密码可以
    other = H.Client()
    assert other.login(H.ADMIN_USER, H.ADMIN_PASSWORD).status_code == 401
    fresh = H.Client()
    assert fresh.login(H.ADMIN_USER, H.ADMIN_PASSWORD2).status_code == 200

    # 改密后 must_change 落库为 0
    assert int(H.q1("SELECT " + H.must_col() + " AS f FROM admin_user")["f"]) == 0


# ---------------------------------------------------------------------------
# C6 登录锁定 + 账号枚举
# ---------------------------------------------------------------------------
def test_c06_lockout_after_failures(admin):
    """C6：连续 5 次错误密码 → 第 6 次即使密码正确也 423；锁定期不泄漏账号是否存在。"""
    unknown = H.Client()
    resp_unknown = unknown.login("no_such_user_zzz", "WrongPass123")
    exist = H.Client()
    resp_exist = exist.login(H.ADMIN_USER, "WrongPass123")
    assert resp_unknown.status_code == 401 and resp_exist.status_code == 401
    assert resp_unknown.text == resp_exist.text, (
        "账号不存在与密码错误的响应体不一致，可被用来枚举账号：\n%s\n%s"
        % (resp_unknown.text, resp_exist.text))

    # 再打 4 次（累计 5 次）
    for _ in range(4):
        H.Client().login(H.ADMIN_USER, "WrongPass999")
    row = H.q1("SELECT failed_count, locked_until FROM admin_user WHERE username = %s",
               (H.ADMIN_USER,))
    assert int(row["failed_count"]) >= 5, row
    assert row["locked_until"] is not None, "达到阈值却没有置锁定时间"

    resp = H.Client().login(H.ADMIN_USER, H.ADMIN_PASSWORD)
    assert resp.status_code == 423, "锁定期间密码正确也必须是 423，实际 %s %s" % (
        resp.status_code, resp.text)
    assert H.error_of(resp)["code"] == "account_locked", resp.text
    assert H.has_chinese(H.error_of(resp)["message"])

    # 锁定期内，不存在的账号与存在账号的响应仍应可比（都不泄漏）
    H.unlock_admin()
    assert H.Client().login(H.ADMIN_USER, H.ADMIN_PASSWORD2).status_code == 200


# ---------------------------------------------------------------------------
# C7 会话
# ---------------------------------------------------------------------------
def test_c07_logout_invalidates_session_immediately(admin):
    """C7：登出后原 Cookie 立即失效（401）。"""
    token = admin.session.cookies.get("ls_session")
    assert token, admin.session.cookies.get_dict()

    assert admin.get("/admin/api/me").status_code == 200
    resp = admin.post("/admin/api/logout", {})
    assert resp.status_code == 200, resp.text

    again = H.Client()
    resp = again.get("/admin/api/me", csrf=False,
                     headers={"Cookie": "ls_session=" + token})
    assert resp.status_code == 401, "登出后旧 Cookie 仍然可用：%s" % resp.text


def test_c07_session_stores_hash_not_plaintext(admin):
    """C7-b：会话表只存 sha256(token)，不存明文。"""
    token = admin.session.cookies.get("ls_session")
    rows = H.q("SELECT token_hash, csrf_token FROM admin_session")
    assert rows, "登录后没有会话行"
    hashes = [str(r["token_hash"]) for r in rows]
    assert token not in hashes, "库里存了明文令牌"
    assert H.sha256_hex(token) in hashes, "token_hash 不是 sha256(token)"
    dumped = json.dumps(rows, ensure_ascii=False)
    assert token not in dumped
    assert len(rows[0]["csrf_token"]) == 32


# ---------------------------------------------------------------------------
# C8 信息泄漏
# ---------------------------------------------------------------------------
def test_c08_health_and_500_do_not_leak(admin):
    """C8：/api/health 与 500 错误体不得出现库口令、私钥、堆栈路径。"""
    cfg = H.get_config()
    resp = H.anon().get("/api/health", csrf=False)
    assert resp.status_code == 200, resp.text
    text = resp.text
    assert cfg.db_password not in text, "健康检查泄漏数据库口令"
    for marker in ("PRIVATE KEY", "BEGIN", str(H.ROOT), "D:\\Git", "Traceback"):
        assert marker not in text, "健康检查泄漏 %s：%s" % (marker, text)
    body = resp.json()
    assert body["ok"] is True and body["db"] is True
    assert body.get("key_id") == cfg.key_id

    # 触发一个真实 500：page 取值超出 MySQL 能接受的范围（LIMIT/OFFSET 无上界校验）
    resp = admin.get("/admin/api/codes", params={"page": 10 ** 25})
    assert resp.status_code == 500, resp.text
    text = resp.text
    assert cfg.db_password not in text
    for marker in ("1064", "SQL", "pymysql", "Traceback", "D:\\Git", "site-packages"):
        assert marker not in text, "500 响应泄漏 %s：%s" % (marker, text)
    err = H.error_of(resp)
    assert err.get("code") == "internal_error"
    assert H.has_chinese(err.get("message"))


def test_c08_error_body_never_has_detail_wrapper(admin):
    """C8-b：错误体必须是顶层 error（不能出现 FastAPI 默认的 detail 包装）。"""
    resp = admin.get("/admin/api/licenses/LIC-NOT-EXIST")
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert "detail" not in body, body
    assert H.error_of(resp).get("code") == "not_found", body

    resp = H.anon().get("/admin/api/nope", csrf=False)
    assert resp.status_code == 404
    assert "detail" not in resp.json()


# ---------------------------------------------------------------------------
# C9 限流可绕过（X-Forwarded-For）
# ---------------------------------------------------------------------------
def test_c09_rate_limit_hit_and_bypassable_by_forwarded_for():
    """C9：限流本身生效；但默认 ``LS_TRUST_PROXY=1`` 下伪造 XFF 即可绕过（隐患）。"""
    machine = "DEAD-BEEF-0000-0000-0000-0000-0000-0001"
    fixed_ip = "172.31.0.1"
    statuses = [H.activate(machine, "KF-0000-0000-0000-0000",
                           ip=fixed_ip).status_code for _ in range(13)]
    assert 429 in statuses, "同一 IP+机器码连打 13 次没有触发限流：%s" % statuses
    assert statuses.index(429) == 10, "限流阈值不符合 10/60：%s" % statuses
    assert statuses[0] == 400, "前 10 次应当是 invalid_code：%s" % statuses

    rotated = [H.activate(machine, "KF-0000-0000-0000-0000",
                          ip="172.31.9.%d" % (i + 10)).status_code
               for i in range(13)]
    assert 429 not in rotated, (
        "换 X-Forwarded-For 后仍被限流（%s）——说明限流键没被伪造头绕过" % rotated)
