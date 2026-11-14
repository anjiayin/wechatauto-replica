# -*- coding: utf-8 -*-
"""pytest 夹具：服务可用性守卫、交付态重置、登录态客户端。

服务地址取环境变量 ``LS_TEST_BASE``，默认 ``http://127.0.0.1:8800``。
若目标地址不可达，夹具会自动用 ``python run.py serve --port 8800`` 拉起一个
临时进程（会话结束时关掉），这样 ``pytest tests`` 一条命令即可复验。
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
_PORT = int(os.environ.get("LS_TEST_PORT", "8800") or "8800")


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
    """确保被测服务在跑（没跑就自己拉起来，结束时收掉）。"""
    proc = None
    if not _healthy():
        assert not _port_open(), (
            "端口 %d 已被其它进程占用但 /api/health 不可用，请先清理" % _PORT)
        proc = subprocess.Popen(
            [sys.executable, "run.py", "serve", "--port", str(_PORT)],
            cwd=str(ROOT), stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT)
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
