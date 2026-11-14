# -*- coding: utf-8 -*-
"""pytest 夹具：测试库准备、专用服务自起、登录态客户端。

铁律：测试一律连**测试库**（``wechat_kefu_test``，见 ``_helpers.TEST_DB_NAME``），
并**总是自起**一个专用服务进程（默认 127.0.0.1:8801，``LS_TEST_PORT`` 可改），
绝不复用已运行的真实服务（8800）——否则测试请求会打到真实库上，污染真实数据。
会话结束时自动收掉临时服务。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _helpers as H  # noqa: E402

ROOT = H.ROOT
_PORT = H.TEST_PORT


def _healthy() -> bool:
    """探活 /api/health。"""
    try:
        resp = H.Client().get("/api/health", csrf=False)
        return resp.status_code in (200, 503)
    except Exception:                                   # noqa: BLE001
        return False


def _port_open() -> bool:
    """端口是否已被占用。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", _PORT)) == 0


@pytest.fixture(scope="session", autouse=True)
def server():
    """准备测试库并自起专用服务（结束时收掉）。"""
    H.ensure_test_db()          # 建库 + 建表 + 幂等迁移（测试库，不影响真实库）
    assert not _port_open(), (
        "端口 %d 已被其它进程占用（可能是真实服务或残留进程）。"
        "为避免测试请求打到真实库，请先清理该端口再跑测试。" % _PORT)
    proc = subprocess.Popen(
        [sys.executable, "run.py", "serve", "--port", str(_PORT)],
        cwd=str(ROOT), stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        env=dict(os.environ))   # LS_DB_NAME 已由 _helpers 指向测试库，子进程继承
    deadline = time.time() + 60
    while time.time() < deadline:
        if _healthy():
            break
        if proc.poll() is not None:
            raise RuntimeError("服务进程启动即退出，退出码 %s" % proc.returncode)
        time.sleep(0.5)
    assert _healthy(), "服务 60 秒内没有起来（%s）" % H.BASE_URL
    yield H.BASE_URL
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:               # pragma: no cover
            proc.kill()


@pytest.fixture()
def admin():
    """交付态 + 已登录且已完成强制改密的管理端客户端。"""
    H.reset_all()
    return H.admin_client()


@pytest.fixture()
def admin_raw():
    """交付态 + 只登录、**未**改密（must_change=1 状态）。"""
    H.reset_all()
    cli = H.Client()
    resp = cli.login()
    assert resp.status_code == 200, resp.text
    return cli
