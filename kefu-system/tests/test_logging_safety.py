# -*- coding: utf-8 -*-
"""Bug B 回归：日志轮转失败不得让进程永久静默。

现场：data/kefu.log 已达上限，Windows 上 RotatingFileHandler.doRollover()
rename 被占用文件时抛 PermissionError；基类此刻已 close 了 stream，异常又被
handleError 吞掉 —— 之后每次 emit 都失败，进程静默（库里在写、日志不再增长）。

覆盖：
- SafeRotatingFileHandler 轮转失败后仍可继续写入；
- stream 丢失（None）后 emit 能自动重新打开；
- setup_logging() 幂等（create_app() 多次调用不叠加 handler），并打印日志
  文件绝对路径与当前大小。

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_logging_safety.py -v
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from app import main as app_main


def _record(msg: str) -> logging.LogRecord:
    """构造一条最小日志记录（避免依赖全局 logger 配置）。"""
    return logging.LogRecord("kefu", logging.INFO, __file__, 1, msg, None, None)


def _new_handler(path) -> app_main.SafeRotatingFileHandler:
    """构造带纯消息格式的 SafeRotatingFileHandler。"""
    handler = app_main.SafeRotatingFileHandler(
        str(path), maxBytes=1024 * 1024, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def test_rollover_failure_keeps_writing(tmp_path, monkeypatch):
    """轮转失败（Windows 文件被占用）后依然继续写日志，不出现静默。"""
    log_path = tmp_path / "kefu.log"
    handler = _new_handler(log_path)
    try:
        handler.emit(_record("轮转前"))

        def _raise_oserror(self) -> None:
            raise PermissionError("文件被另一个进程占用（模拟 Windows rename 失败）")

        monkeypatch.setattr(RotatingFileHandler, "doRollover", _raise_oserror)
        handler.doRollover()          # 不得抛出异常
        assert handler.stream is not None
        handler.emit(_record("轮转失败后"))
    finally:
        handler.close()
    content = log_path.read_text(encoding="utf-8")
    assert "轮转前" in content
    assert "轮转失败后" in content


def test_emit_reopens_when_stream_lost(tmp_path):
    """stream 被置空（轮转/异常后）时 emit 自动重新打开，日志不丢。"""
    log_path = tmp_path / "kefu.log"
    handler = _new_handler(log_path)
    handler.emit(_record("第一次"))
    if handler.stream is not None:
        handler.stream.close()
    handler.stream = None
    handler.emit(_record("重新打开后"))
    handler.close()
    content = log_path.read_text(encoding="utf-8")
    assert "第一次" in content
    assert "重新打开后" in content


def test_setup_logging_is_idempotent_and_reports_path(tmp_path, monkeypatch):
    """重复调用 setup_logging 不叠加 handler，并打印日志文件路径与大小。"""
    root = logging.getLogger()
    saved = [h for h in root.handlers
             if getattr(h, "_kefu_file", False) or getattr(h, "_kefu_console", False)]
    for handler in saved:                      # 隔离：先摘掉全局已有 handler
        root.removeHandler(handler)

    monkeypatch.setattr(app_main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_main, "LOG_PATH", tmp_path / "kefu.log")
    try:
        app_main.setup_logging()
        app_main.setup_logging()
        file_handlers = [h for h in root.handlers if getattr(h, "_kefu_file", False)]
        consoles = [h for h in root.handlers if getattr(h, "_kefu_console", False)]
        assert len(file_handlers) == 1
        assert len(consoles) == 1
        content = (tmp_path / "kefu.log").read_text(encoding="utf-8")
        assert "日志文件" in content            # 启动自检行（绝对路径 + 大小）
        assert "kefu.log" in content
    finally:                                   # 还原全局 handler，避免影响其他测试
        for handler in list(root.handlers):
            if getattr(handler, "_kefu_file", False) or \
                    getattr(handler, "_kefu_console", False):
                root.removeHandler(handler)
                handler.close()
        for handler in saved:
            root.addHandler(handler)
