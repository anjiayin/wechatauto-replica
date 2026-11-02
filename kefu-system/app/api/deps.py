# -*- coding: utf-8 -*-
"""FastAPI 依赖注入：持有应用级单例（由 main.py lifespan 组装注入）。"""
from __future__ import annotations

import threading

from fastapi import HTTPException

from app.db import Database
from app.notifier import Notifier
from app.pipeline import Pipeline
from app.sender import Sender
from app.wechat_service import WeChatService

_lock = threading.Lock()
_db: Database | None = None
_wechat: WeChatService | None = None
_pipeline: Pipeline | None = None
_sender: Sender | None = None
_notifier: Notifier | None = None


def configure(db: Database, wechat: WeChatService, pipeline: Pipeline,
              sender: Sender, notifier: Notifier) -> None:
    """应用启动时注入全部单例（main.py 调用）。"""
    global _db, _wechat, _pipeline, _sender, _notifier
    with _lock:
        _db = db
        _wechat = wechat
        _pipeline = pipeline
        _sender = sender
        _notifier = notifier


def _require(instance, name: str):
    """单例缺失时给出中文 500（正常不会发生，防御性兜底）。"""
    if instance is None:
        raise HTTPException(status_code=500,
                            detail=f"系统尚未完成初始化（{name}）")
    return instance


def get_db() -> Database:
    """业务数据库单例。"""
    return _require(_db, "数据库")


def get_wechat_service() -> WeChatService:
    """微信接入服务单例。"""
    return _require(_wechat, "微信服务")


def get_pipeline() -> Pipeline:
    """分析管线单例。"""
    return _require(_pipeline, "分析管线")


def get_sender() -> Sender:
    """发送服务单例。"""
    return _require(_sender, "发送服务")


def get_notifier() -> Notifier:
    """WS 通知中心单例。"""
    return _require(_notifier, "通知中心")
