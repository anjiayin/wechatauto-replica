# -*- coding: utf-8 -*-
"""QA 复验公共设施（**只被 tests/ 使用，不改 app/ 任何源码**）。

提供四类能力：
1. ``Client``：绕过本机 ``HTTP_PROXY`` 的 HTTP 客户端（否则打不到 127.0.0.1），
   每次请求自带独立 ``X-Forwarded-For``，避免进程内滑动窗口限流把测试用例互相打死；
2. 数据库直查（``q`` / ``q1`` / ``x``）：断言"库里到底发生了什么"，而不是只看接口回包；
3. ``reset_all``：把库恢复成交付态（业务表清空 + admin / must_change=1）；
4. ``client_side``：**在 kefu-system 目录下用子进程**调用真正的客户端
   ``app.license.payload_bytes / verify_license / extract_error``，
   避免与服务端 ``app`` 包同名冲突，也避免"用服务端自己的函数验自己"的假阳性。
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pymysql
import requests
from pymysql.cursors import DictCursor

ROOT = Path(__file__).resolve().parent.parent          # license-server/
KEFU = ROOT.parent / "kefu-system"                     # 客户端仓库（只读）
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import security, signing                      # noqa: E402
from app.config import get_config                      # noqa: E402

BASE_URL = os.environ.get("LS_TEST_BASE", "http://127.0.0.1:8800").rstrip("/")

#: 交付态管理员（.env / README 约定）
ADMIN_USER = "admin"
ADMIN_PASSWORD = "Admin98765!"
#: 复验过程中改成的密码（改完 must_change 归 0，业务接口才可用）
ADMIN_PASSWORD2 = "QaPassw0rd-9x"

#: 三个合法机器码（32 位十六进制，分组形式）
M1 = "BBCF-BCA8-F127-5747-05D8-46B3-E8D8-CAF0"
M2 = "1111-2222-3333-4444-5555-6666-7777-8888"
M3 = "AAAA-BBBB-CCCC-DDDD-EEEE-FFFF-0000-1111"


def compact(machine: str) -> str:
    """去掉横杠（模拟客户端另一种上报形式）。"""
    return machine.replace("-", "")


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
def db():
    """新建一条直连（autocommit，避免测试里残留事务）。"""
    cfg = get_config()
    return pymysql.connect(
        host=cfg.db_host, port=cfg.db_port, user=cfg.db_user,
        password=cfg.db_password, database=cfg.db_name, charset=cfg.db_charset,
        autocommit=True, cursorclass=DictCursor, connect_timeout=cfg.db_connect_timeout)


def q(sql: str, params: Optional[Tuple[Any, ...]] = None) -> List[Dict[str, Any]]:
    """查询多行。"""
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall() or [])


def q1(sql: str, params: Optional[Tuple[Any, ...]] = None) -> Optional[Dict[str, Any]]:
    """查询单行。"""
    rows = q(sql, params)
    return rows[0] if rows else None


def x(sql: str, params: Optional[Tuple[Any, ...]] = None) -> int:
    """执行写语句，返回受影响行数。"""
    with db() as conn:
        with conn.cursor() as cur:
            return int(cur.execute(sql, params) or 0)


_MUST_COL: Optional[str] = None


def must_col() -> str:
    """``admin_user`` 里"强制改密"那一列的真实列名（不硬编码拼写）。"""
    global _MUST_COL
    if _MUST_COL is None:
        cols = [str(row["Field"]) for row in q("SHOW COLUMNS FROM admin_user")]
        _MUST_COL = next(c for c in cols if c.startswith("must_"))
    return _MUST_COL


def admin_id(username: str = ADMIN_USER) -> int:
    """取管理员 id。"""
    row = q1("SELECT id FROM admin_user WHERE username = %s", (username,))
    return int(row["id"]) if row else 0


def unlock_admin() -> None:
    """清零失败计数与锁定（锁定用例结束后复位）。"""
    x("UPDATE admin_user SET failed_count = 0, locked_until = NULL")


def reset_all() -> None:
    """恢复交付态：业务表清空，管理员只留 admin/Admin98765!/强制改密=1。"""
    with db() as conn:
        with conn.cursor() as cur:
            for table in ("license", "license_code", "customer", "audit_log",
                          "admin_session"):
                cur.execute("TRUNCATE TABLE " + table)
            cur.execute("DELETE FROM admin_user")
            now = signing.now()
            col = must_col()
            cur.execute(
                "INSERT INTO admin_user (username, password_hash, display_name, "
                "role, status, " + col + ", failed_count, locked_until, "
                "last_login_at, last_login_ip, password_updated_at, "
                "created_at, updated_at) "
                "VALUES (%s, %s, %s, 'admin', 1, 1, 0, NULL, NULL, '', %s, %s, %s)",
                (ADMIN_USER, security.hash_password(ADMIN_PASSWORD),
                 "系统管理员", now, now, now))
            cur.execute(
                "INSERT INTO audit_log (actor_id, actor, action, target_type, "
                "target_id, machine_code, days_delta, before_json, after_json, "
                "ip, note, created_at) VALUES (0, 'system', 'bootstrap_admin', "
                "'admin', %s, '', 0, NULL, NULL, '', '首启自动创建管理员账号', %s)",
                (ADMIN_USER, now))


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------
_IP_SEQ = itertools.count(1)


def next_ip() -> str:
    """生成一个未使用过的客户端 IP（配合 LS_TRUST_PROXY=1 规避 IP 限流）。"""
    n = next(_IP_SEQ)
    return "10.66.%d.%d" % ((n // 250) % 250 + 1, (n % 250) + 1)


class Client:
    """带会话 / CSRF 的最小 HTTP 客户端。"""

    def __init__(self, ip: Optional[str] = None) -> None:
        self.session = requests.Session()
        self.session.trust_env = False        # 忽略 HTTP_PROXY（本机 127.0.0.1 直连）
        self.ip = ip or next_ip()
        self.csrf = ""
        self.user: Dict[str, Any] = {}

    # -- 底层 ---------------------------------------------------------
    def request(self, method: str, path: str, *, body: Any = None,
                params: Optional[Dict[str, Any]] = None,
                csrf: bool = True,
                headers: Optional[Dict[str, str]] = None) -> requests.Response:
        url = path if path.startswith("http") else BASE_URL + path
        hdrs = {"X-Forwarded-For": self.ip}
        if csrf and self.csrf:
            hdrs["X-CSRF-Token"] = self.csrf
        if headers:
            hdrs.update(headers)
        return self.session.request(method, url, json=body, params=params,
                                    headers=hdrs, timeout=30)

    def get(self, path: str, **kw: Any) -> requests.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> requests.Response:
        return self.request("POST", path, body=body if body is not None else {}, **kw)

    # -- 业务封装 -----------------------------------------------------
    def login(self, username: str = ADMIN_USER,
              password: str = ADMIN_PASSWORD) -> requests.Response:
        resp = self.post("/admin/api/login",
                         {"username": username, "password": password}, csrf=False)
        if resp.status_code == 200:
            data = resp.json()
            self.csrf = str(data.get("csrf_token") or "")
            self.user = dict(data.get("user") or {})
        return resp

    def change_password(self, old: str = ADMIN_PASSWORD,
                        new: str = ADMIN_PASSWORD2) -> requests.Response:
        return self.post("/admin/api/password",
                         {"old_password": old, "new_password": new})


def anon() -> Client:
    """未登录客户端。"""
    return Client()


def admin_client(change_password: bool = True) -> Client:
    """登录的管理员客户端；默认顺手把强制改密走完（业务接口才可用）。"""
    cli = Client()
    resp = cli.login()
    assert resp.status_code == 200, "登录失败：%s %s" % (resp.status_code, resp.text)
    if change_password:
        resp = cli.change_password()
        assert resp.status_code == 200, "改密失败：%s %s" % (resp.status_code, resp.text)
    return cli


# ---------------------------------------------------------------------------
# 客户端侧（kefu-system）验签 —— 子进程调用，避免 app 包同名冲突
# ---------------------------------------------------------------------------
_CLIENT_SNIPPET = r'''
import base64, hashlib, json, sys
from app.license import extract_error, payload_bytes, verify_license

req = json.load(sys.stdin)
out = {}
payload = req.get("payload")
if payload is not None:
    raw = payload_bytes(payload)
    out["bytes_sha256"] = hashlib.sha256(raw).hexdigest()
    out["bytes_hex"] = raw.hex()
doc = req.get("doc")
if doc is not None:
    ok, reason = verify_license(doc)
    out["verify_ok"] = bool(ok)
    out["verify_reason"] = str(reason)
body = req.get("error_body")
if body is not None:
    code, message = extract_error(body)
    out["error_code"] = code
    out["error_message"] = message
sys.stdout.write(json.dumps(out, ensure_ascii=False))
'''


def client_side(payload: Optional[Dict[str, Any]] = None,
                doc: Optional[Dict[str, Any]] = None,
                error_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """在 kefu-system 目录下执行客户端代码，返回签名原文摘要与验签结果。"""
    assert (KEFU / "app" / "license.py").exists(), "找不到客户端 app/license.py"
    request = {"payload": payload, "doc": doc, "error_body": error_body}
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, "-c", _CLIENT_SNIPPET], cwd=str(KEFU),
        input=json.dumps(request, ensure_ascii=False),
        capture_output=True, text=True, encoding="utf-8", timeout=180, env=env)
    assert proc.returncode == 0, "客户端模块执行失败：%s" % (proc.stderr or "")[-800:]
    return json.loads(proc.stdout)


def verify_with_client(doc: Dict[str, Any]) -> Tuple[bool, str]:
    """用客户端 ``verify_license`` 验签，返回 ``(ok, 原因)``。"""
    out = client_side(payload=doc.get("payload"), doc=doc)
    return bool(out.get("verify_ok")), str(out.get("verify_reason") or "")


# ---------------------------------------------------------------------------
# 业务动作封装
# ---------------------------------------------------------------------------
def create_codes(cli: Client, count: int = 1, days: int = 365,
                 customer: str = "测试客户", phone: str = "",
                 edition: str = "standard", note: str = "") -> List[str]:
    """管理端生成激活码，返回码列表。"""
    resp = cli.post("/admin/api/codes",
                    {"count": count, "days": days, "customer": customer,
                     "customer_phone": phone, "edition": edition, "note": note})
    assert resp.status_code == 200, "生成激活码失败：%s %s" % (
        resp.status_code, resp.text)
    return [str(item["code"]) for item in resp.json()["items"]]


def activate(machine: str, code: str = "", ip: Optional[str] = None,
             version: str = "1.0.0", os_name: str = "Windows 10") -> requests.Response:
    """调客户端激活接口（每次独立 IP，避开激活限流）。"""
    cli = Client(ip)
    return cli.post("/api/v1/license/activate",
                    {"machine_code": machine, "activation_code": code,
                     "app_version": version, "os": os_name}, csrf=False)


def heartbeat(machine: str, license_id: str = "",
              ip: Optional[str] = None) -> requests.Response:
    """调客户端心跳接口。"""
    cli = Client(ip)
    return cli.post("/api/v1/license/heartbeat",
                    {"machine_code": machine, "license_id": license_id,
                     "app_version": "1.0.0"}, csrf=False)


def activate_ok(machine: str, code: str = "") -> Dict[str, Any]:
    """激活并断言成功，返回许可证文档。"""
    resp = activate(machine, code)
    assert resp.status_code == 200, "激活失败：%s %s" % (resp.status_code, resp.text)
    data = resp.json()
    assert data.get("ok") is True, data
    return dict(data["license"])


def license_row(license_id: str) -> Optional[Dict[str, Any]]:
    """按许可证号直查库。"""
    return q1("SELECT * FROM license WHERE license_id = %s", (license_id,))


def audit_rows(action: str = "", target_id: str = "") -> List[Dict[str, Any]]:
    """按动作/目标查审计。"""
    sql = "SELECT * FROM audit_log WHERE 1 = 1"
    params: List[Any] = []
    if action:
        sql += " AND action = %s"
        params.append(action)
    if target_id:
        sql += " AND target_id = %s"
        params.append(target_id)
    sql += " ORDER BY id DESC"
    return q(sql, tuple(params))


# ---------------------------------------------------------------------------
# 直接造数（统计口径用，绕开接口只为构造精确数字）
# ---------------------------------------------------------------------------
_LICENSE_COLUMNS = (
    "license_id", "machine_code", "customer_id", "customer_name",
    "customer_phone", "edition", "activation_code", "issued_at", "expires_at",
    "grace_days", "features", "revoked_at", "superseded_at", "reissued_from",
    "last_seen_at", "app_version", "os", "note", "current_key", "created_at",
    "updated_at")

_LICENSE_SEQ = itertools.count(1)


def insert_license(**kw: Any) -> str:
    """按字段直接插一条许可证（缺省值保证能通过 NOT NULL 约束）。"""
    now = signing.now()
    seq = next(_LICENSE_SEQ)
    row: Dict[str, Any] = {
        "license_id": "LIC-TEST-%04d" % seq,
        "machine_code": "FFFF-0000-0000-0000-0000-0000-0000-%04d" % seq,
        "customer_id": 0, "customer_name": "", "customer_phone": "",
        "edition": "standard", "activation_code": "",
        "issued_at": now - timedelta(days=1),
        "expires_at": now + timedelta(days=365),
        "grace_days": 7, "features": "monitor,reply,ai",
        "revoked_at": None, "superseded_at": None, "reissued_from": "",
        "last_seen_at": now, "app_version": "1.0.0", "os": "Windows 10",
        "note": "", "current_key": None, "created_at": now, "updated_at": now,
    }
    row.update(kw)
    if "current_key" not in kw:
        row["current_key"] = (row["machine_code"]
                              if not row["revoked_at"] and not row["superseded_at"]
                              else None)
    cols = ", ".join(_LICENSE_COLUMNS)
    marks = ", ".join(["%s"] * len(_LICENSE_COLUMNS))
    x("INSERT INTO license (" + cols + ") VALUES (" + marks + ")",
      tuple(row[c] for c in _LICENSE_COLUMNS))
    return str(row["license_id"])


_CODE_SEQ = itertools.count(1)


def insert_code(**kw: Any) -> str:
    """按字段直接插一条激活码。"""
    now = signing.now()
    seq = next(_CODE_SEQ)
    row: Dict[str, Any] = {
        "code": "KF-TEST-%04d" % seq, "days": 365, "edition": "standard",
        "customer_id": 0, "customer_name": "", "customer_phone": "",
        "batch": "BTEST", "note": "", "used_machine": "", "used_at": None,
        "created_by": "qa", "created_at": now,
    }
    row.update(kw)
    x("INSERT INTO license_code (code, days, edition, customer_id, "
      "customer_name, customer_phone, batch, note, used_machine, used_at, "
      "created_by, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
      "%s, %s, %s)",
      (row["code"], row["days"], row["edition"], row["customer_id"],
       row["customer_name"], row["customer_phone"], row["batch"], row["note"],
       row["used_machine"], row["used_at"], row["created_by"],
       row["created_at"]))
    return str(row["code"])


# ---------------------------------------------------------------------------
# 断言小工具
# ---------------------------------------------------------------------------
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")


def has_chinese(text: Any) -> bool:
    """是否包含中文字符。"""
    return bool(_HAN_RE.search(str(text or "")))


def error_of(resp: requests.Response) -> Dict[str, Any]:
    """取顶层 ``{"error":{code,message}}``（不存在返回空字典）。"""
    try:
        data = resp.json()
    except ValueError:
        return {}
    inner = data.get("error")
    return inner if isinstance(inner, dict) else {}


def is_iso_cn(value: Any) -> bool:
    """是否是带 +08:00 的秒级 ISO 字符串。"""
    text = str(value or "")
    if not text.endswith("+08:00"):
        return False
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return True


def parse_iso(value: Any) -> datetime:
    """解析接口返回的 ISO 时间（带 +08:00，结果保留时区）。"""
    return datetime.fromisoformat(str(value))


def parse_iso_naive(value: Any) -> datetime:
    """解析 ISO 并去掉时区：服务端内部时间一律东八区 naive，比对前先归一。"""
    return parse_iso(value).replace(tzinfo=None)


def must_flag(user: Dict[str, Any]) -> Optional[bool]:
    """从登录/me 返回的 user 里取"强制改密"标记（不硬编码字段名）。"""
    known = {"id", "username", "display_name", "role"}
    for key, value in user.items():
        if key not in known:
            return bool(value)
    return None


def frontend_must_change_code() -> str:
    """从 ``app.js`` 里抠出前端识别的"强制改密"错误码（前后端一致性验证）。"""
    text = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    found = re.findall(r"errCode\s*===\s*'([A-Za-z0-9_]+)'", text)
    assert found, "app.js 里没有找到强制改密错误码"
    return found[0]


def sha256_hex(text: str) -> str:
    """会话令牌哈希（与库里的 token_hash 比对）。"""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()
