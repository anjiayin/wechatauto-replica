# -*- coding: utf-8 -*-
"""E 组：前端与运维入口。

对齐依据：``docs/design.md`` §5.4（页面路由）、§8.5 / §10.1（run.py 子命令）、
``app/main.py``（静态挂载顺序）、``app/signing.py``（私钥缺失必须中文报错退出）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest
import requests

import _helpers as H

PAGES = [
    "/admin/",
    "/admin/index.html",
    "/admin/login.html",
    "/admin/codes.html",
    "/admin/licenses.html",
    "/admin/customers.html",
    "/admin/audit.html",
    "/admin/account.html",
    "/admin/app.js",
    "/admin/style.css",
]


# ---------------------------------------------------------------------------
# E1 页面与静态资源
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", PAGES)
def test_e01_pages_are_served(path):
    """E1：7 个页面 + app.js + style.css 均 200。"""
    resp = requests.get(H.BASE_URL + path, timeout=20,
                        proxies={"http": None, "https": None})
    assert resp.status_code == 200, "%s → %s" % (path, resp.status_code)
    assert resp.content, "%s 返回空内容" % path


def test_e01_root_redirects_to_admin():
    """E1-b：/ 必须 302 跳到 /admin/。"""
    resp = requests.get(H.BASE_URL + "/", timeout=20, allow_redirects=False,
                        proxies={"http": None, "https": None})
    assert resp.status_code in (301, 302, 307, 308), resp.status_code
    assert resp.headers.get("location", "").endswith("/admin/"), resp.headers


def test_e01_admin_api_not_shadowed_by_static():
    """E1-c：静态目录挂载没有抢走 /admin/api/* 路由。"""
    resp = H.anon().get("/admin/api/dashboard", csrf=False)
    assert resp.status_code == 401, resp.text
    assert resp.headers.get("content-type", "").startswith("application/json")


def test_e02_no_innerhtml_with_user_data():
    """E2：页面里不得把用户数据用 innerHTML 直插（防存储型 XSS）。"""
    static = H.ROOT / "app" / "static"
    for name in ("licenses.html", "customers.html", "audit.html", "index.html",
                 "codes.html", "account.html", "login.html", "app.js"):
        text = (static / name).read_text(encoding="utf-8")
        hits = re.findall(r"\.innerHTML\s*(?:=|\+=)", text)
        assert not hits, "%s 里出现 innerHTML 赋值：%s" % (name, hits)
        hits = re.findall(r"insertAdjacentHTML\s*\(", text)
        assert not hits, "%s 里出现 insertAdjacentHTML" % name

    # 页面统一走共享的 h()/append() 构造 DOM，内部用 textContent 落文本
    app_js = (static / "app.js").read_text(encoding="utf-8")
    assert "el.textContent" in app_js, "h() 没有用 textContent 落文本"
    assert "createTextNode" in app_js, "append() 没有用 createTextNode 落文本"

    for name in ("licenses.html", "customers.html", "audit.html"):
        text = (static / name).read_text(encoding="utf-8")
        assert "textContent" in text or "h(" in text, (
            "%s 既没有 textContent 也没有用共享 h() 构造 DOM" % name)
        assert "text:" in text or "textContent" in text, (
            "%s 的取值没有走 text/textContent" % name)
        assert "document.write" not in text, "%s 用了 document.write" % name


def test_e02_frontend_uses_csrf_header():
    """E2-b：前端写请求必须带 X-CSRF-Token（否则后台写操作全 403）。"""
    text = (H.ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "X-CSRF-Token" in text
    assert "credentials" in text and "same-origin" in text


# ---------------------------------------------------------------------------
# E3 运维入口
# ---------------------------------------------------------------------------
def _run(args, env_extra=None, timeout=180):
    """在项目根目录跑一条 python 命令。"""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable] + args, cwd=str(H.ROOT), env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=timeout)


def test_e03_init_db_is_idempotent():
    """E3：init-db 连跑两次都成功，且仍识别出 6 张表。"""
    first = _run(["run.py", "init-db"])
    assert first.returncode == 0, first.stdout + first.stderr
    assert "6 张表" in first.stdout, first.stdout
    assert "uk_current_machine" in first.stdout
    assert "存在" in first.stdout

    second = _run(["run.py", "init-db"])
    assert second.returncode == 0, second.stdout + second.stderr
    assert "6 张表" in second.stdout

    tables = [str(list(row.values())[0]) for row in H.q("SHOW TABLES")]
    assert set(tables) >= {"admin_user", "admin_session", "customer",
                           "license_code", "license", "audit_log"}, tables


def test_e03_check_passes():
    """E3-b：run.py check 自检通过（含与客户端 payload_bytes 逐字节比对）。"""
    proc = _run(["run.py", "check"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "验签：OK" in proc.stdout, proc.stdout
    assert "客户端比对：OK" in proc.stdout, proc.stdout
    assert "全部自检通过" in proc.stdout, proc.stdout


def test_e03_missing_private_key_fails_with_chinese_reason():
    """E3-c：私钥缺失时必须启动失败并给出中文原因（不能静默启动）。"""
    proc = _run(["-c", "import app.main"], env_extra={
        "LS_PRIVATE_KEY_PATH": "Z:/no/such/dir/license_private.pem"})
    assert proc.returncode != 0, "私钥缺失却启动成功（%s）" % proc.stdout
    combined = proc.stdout + proc.stderr
    assert H.has_chinese(combined), combined
    assert ("私钥" in combined), combined
    assert "Traceback" not in combined, "读私钥失败不应该抛裸栈：%s" % combined


def test_e03_create_admin_cli():
    """E3-d：create-admin 能建号，且账号可用（改密策略生效）。"""
    proc = _run(["run.py", "create-admin", "--username", "qa_ops",
                 "--password", "QaOps12345"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "qa_ops" in proc.stdout

    cli = H.Client()
    resp = cli.login("qa_ops", "QaOps12345")
    assert resp.status_code == 200, resp.text
    # 弱口令必须被拒
    proc = _run(["run.py", "create-admin", "--username", "qa_ops2",
                 "--password", "123"])
    assert proc.returncode != 0, proc.stdout
    assert H.has_chinese(proc.stderr), proc.stderr


def test_e03_issue_code_cli():
    """E3-e：issue-code 能在命令行造码并落库。"""
    H.reset_all()
    proc = _run(["run.py", "issue-code", "--days", "30", "--count", "2",
                 "--phone", "13900009999", "--customer", "命令行客户"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rows = H.q("SELECT code, days, customer_phone FROM license_code")
    assert len(rows) == 2, rows
    assert all(int(r["days"]) == 30 for r in rows)
    assert all(r["customer_phone"] == "13900009999" for r in rows)
