# -*- coding: utf-8 -*-
"""Windows 桌面提醒（交给常驻托盘启动器弹系统通知）。

后端进程没有托盘图标，不能直接弹 Shell 通知；常驻的 ``launcher.py`` 有。
两端通过 ``data/native_alert.json`` 交接：后端原子写入一条提醒，launcher
轮询读取、删除并弹气泡，点击气泡打开
``http://127.0.0.1:<port>/?opportunity=<id>``。

只在操作页面没有任何 WS 连接（页面未打开）时使用；页面已打开时仍走
页面内 toast / 浏览器通知，避免重复提醒。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from app.config import DATA_DIR

logger = logging.getLogger(__name__)

# 与 packaging/launcher.py 的 ALERT_FILE_NAME 保持一致
ALERT_FILE_NAME = "native_alert.json"
# 气泡正文上限（launcher 端还会按 Win32 结构体再截断一次）
BODY_MAX_CHARS = 200


def alert_path() -> Path:
    """提醒请求文件路径（唯一真源：后端 DATA_DIR）。"""
    return Path(DATA_DIR) / ALERT_FILE_NAME


def alert_title(type_name: Any) -> str:
    """提醒标题：与前端 opportunityAlertTitle 语义一致。"""
    name = str(type_name or "").strip()
    if not name or name == "未知类型":
        return "收到新商机"
    return f"收到新的{name}"


def _first_line(text: Any) -> str:
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def build_alert(opportunity: dict) -> dict:
    """把商机记录转成 launcher 可消费的提醒请求。"""
    session_name = str(opportunity.get("session_name") or "").strip()
    body = "：".join(
        part for part in (session_name, _first_line(opportunity.get("summary")))
        if part)
    return {
        "id": int(opportunity["id"]),
        "title": alert_title(opportunity.get("type_name")),
        "body": body[:BODY_MAX_CHARS],
        "at": int(time.time()),
    }


def request_desktop_alert(opportunity: dict) -> bool:
    """写入一条桌面提醒请求（原子替换）；任何异常只记日志，不影响主流程。"""
    try:
        alert = build_alert(opportunity)
        directory = Path(DATA_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".native_alert-", suffix=".tmp", dir=str(directory))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(alert, fh, ensure_ascii=False)
            os.replace(tmp_name, alert_path())
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except (OSError, TypeError, ValueError, KeyError):
        logger.exception("写入桌面提醒失败（已忽略）")
        return False
    logger.info("已请求桌面提醒：商机 #%s（%s）", alert["id"], alert["title"])
    return True