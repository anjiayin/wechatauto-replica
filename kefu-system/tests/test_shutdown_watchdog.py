# -*- coding: utf-8 -*-
"""退出诊断（Ctrl+C 卡在 "Shutting down"）的单元测试。

覆盖：

- ``_dump_all_threads``：包含所有线程段、主线程排第一并标 ``[main]``。
- ``_write_dump``：按 ``shutdown-dump-<时间>.log`` 落盘；目录不可写时返回空串。
- ``_start_exit_watchdog``：超时后调用注入的 ``exit_fn(130)``；重复调用不重复
  起线程（返回 False）。
- ``_KefuServer.handle_exit``：先挂看门狗，再把信号交给 uvicorn（置 should_exit）。

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_shutdown_watchdog.py -v --basetemp=D:/tmp/kefu-sd
"""
from __future__ import annotations

import threading
import time

import uvicorn

import run as run_module


def _reset_watchdog(monkeypatch) -> None:
    """把模块级「已启动」标记复位（每个用例独立）。"""
    monkeypatch.setattr(run_module, "_watchdog_started", False)


def test_dump_all_threads_contains_main_first(monkeypatch):
    """快照里有主线程段，且排在第一段。"""
    text = run_module._dump_all_threads("用例")
    assert "线程栈快照（用例）" in text
    assert "main]" in text
    head = text.split("--- ")[1]
    assert "main]" in head


def test_dump_all_threads_includes_extra_thread(monkeypatch):
    """其它线程也在快照里（线程名可见）。"""
    ready = threading.Event()
    release = threading.Event()

    def _worker() -> None:
        ready.set()
        release.wait(5)

    th = threading.Thread(target=_worker, name="kefu-test-worker",
                          daemon=True)
    th.start()
    try:
        assert ready.wait(5)
        text = run_module._dump_all_threads("带工作线程")
        assert "kefu-test-worker" in text
    finally:
        release.set()
        th.join(timeout=5)


def test_write_dump_creates_file(tmp_path):
    """落盘到指定目录，文件名带 shutdown-dump 前缀。"""
    path = run_module._write_dump("hello 栈", tmp_path)
    assert path
    p = tmp_path / path.split("\\")[-1]
    assert p.exists()
    assert p.name.startswith("shutdown-dump-")
    assert "hello 栈" in p.read_text(encoding="utf-8")


def test_write_dump_bad_dir_returns_empty(tmp_path):
    """目录不可写时不抛异常，返回空串（退出路径绝不能因此再挂一次）。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    assert run_module._write_dump("x", blocker / "sub") == ""


def test_watchdog_calls_exit_after_timeout(monkeypatch, tmp_path, capsys):
    """超时后调用注入的 exit_fn(130)，并把栈写进指定目录。"""
    _reset_watchdog(monkeypatch)
    called: dict = {}
    done = threading.Event()

    def fake_exit(code: int) -> None:
        called["code"] = code
        done.set()

    assert run_module._start_exit_watchdog(
        timeout_sec=0.05, exit_fn=fake_exit, dump_dir=tmp_path) is True
    assert done.wait(5)
    assert called["code"] == 130
    assert any(tmp_path.glob("shutdown-dump-*.log"))
    assert "正在退出" in capsys.readouterr().out


def test_watchdog_only_starts_once(monkeypatch, tmp_path):
    """重复调用不重复起线程（第二次返回 False）。"""
    _reset_watchdog(monkeypatch)
    done = threading.Event()
    assert run_module._start_exit_watchdog(
        timeout_sec=0.05, exit_fn=lambda code: done.set(),
        dump_dir=tmp_path) is True
    assert run_module._start_exit_watchdog(
        timeout_sec=0.05, exit_fn=lambda code: done.set(),
        dump_dir=tmp_path) is False
    assert done.wait(5)


def test_server_handle_exit_starts_watchdog(monkeypatch):
    """收到退出信号：先挂看门狗，再让 uvicorn 置 should_exit。"""
    _reset_watchdog(monkeypatch)
    hits: list[str] = []
    monkeypatch.setattr(run_module, "_start_exit_watchdog",
                        lambda *a, **kw: hits.append("watchdog") or True)
    server = run_module._KefuServer(uvicorn.Config(
        "run:main", log_level="warning"))
    assert server.should_exit is False
    server.handle_exit(2, None)
    assert hits == ["watchdog"]
    assert server.should_exit is True

    # 第二次 Ctrl+C 走 force_exit（uvicorn 的语义）
    server.handle_exit(2, None)
    assert server.force_exit is True


def test_graceful_timeout_is_bounded():
    """优雅退出上限必须是有限值（None = 无限等，就是本次卡死的根因）。"""
    assert run_module.GRACEFUL_SHUTDOWN_SEC == 5
    assert 0 < run_module.EXIT_WATCHDOG_SEC <= 60
    assert run_module.EXIT_WATCHDOG_SEC > run_module.GRACEFUL_SHUTDOWN_SEC
    time.sleep(0)      # 保持 import time 有用，避免 lint 警告
