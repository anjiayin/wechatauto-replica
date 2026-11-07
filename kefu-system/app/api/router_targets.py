# -*- coding: utf-8 -*-
"""监控对象 CRUD 路由。

路由（设计文档 3.2）：
- GET    /api/monitor-targets
- POST   /api/monitor-targets           {username} → 目标对象
- PATCH  /api/monitor-targets/{id}      {enabled}
- DELETE /api/monitor-targets/{id}      → 204
"""
from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Response

from app.api.deps import get_db, get_wechat_service
from app.config import MAX_MONITOR_TARGETS
from app.db import Database
from app.schemas import MonitorTargetCreate, MonitorTargetOut, MonitorTargetUpdate
from app.wechat_service import WeChatService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/monitor-targets", tags=["monitor-targets"])


@router.get("", response_model=list[MonitorTargetOut])
def list_targets(db: Database = Depends(get_db)) -> list[MonitorTargetOut]:
    """已配置监控对象列表。"""
    return [MonitorTargetOut(**t) for t in db.list_targets()]


@router.post("", response_model=MonitorTargetOut, status_code=201)
def create_target(body: MonitorTargetCreate,
                  db: Database = Depends(get_db),
                  svc: WeChatService = Depends(get_wechat_service)
                  ) -> MonitorTargetOut:
    """新增监控对象并即时注册监听。

    显示名通过微信联系人库解析；kind 由 username 形态判定（@chatroom = 群）。
    """
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="会话 username 不能为空")
    if db.get_target_by_username(username):
        raise HTTPException(status_code=400, detail="该会话已在监控列表中")
    kind = "group" if username.endswith("@chatroom") else "friend"
    display_name = svc.get_display_name(username) or username
    # 原子「配额校验 + 插入」（BUG-3 修复：check-then-insert 并发越界）
    try:
        target = db.insert_target_within_limit(username, display_name, kind,
                                               MAX_MONITOR_TARGETS)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="该会话已在监控列表中")
    if target is None:
        raise HTTPException(
            status_code=400,
            detail=f"监控对象已达上限 {MAX_MONITOR_TARGETS} 个，"
                   "请先移除部分再添加")
    if target["enabled"]:
        svc.register_target(username)
    logger.info("新增监控对象：%s（%s）", display_name, username)
    return MonitorTargetOut(**target)


@router.patch("/{target_id}", response_model=MonitorTargetOut)
def update_target(target_id: int, body: MonitorTargetUpdate,
                  db: Database = Depends(get_db),
                  svc: WeChatService = Depends(get_wechat_service)
                  ) -> MonitorTargetOut:
    """启用/停用监控对象（联动 Listener 增删回调）。"""
    target = db.get_target(target_id)
    if not target:
        raise HTTPException(status_code=404, detail="监控对象不存在")
    db.update_target_enabled(target_id, body.enabled)
    if body.enabled:
        svc.register_target(target["username"])
    else:
        svc.unregister_target(target["username"])
    logger.info("监控对象 %s 已%s", target["username"],
                "启用" if body.enabled else "停用")
    return MonitorTargetOut(**db.get_target(target_id))  # type: ignore[arg-type]


@router.delete("/{target_id}", status_code=204)
def delete_target(target_id: int,
                  db: Database = Depends(get_db),
                  svc: WeChatService = Depends(get_wechat_service),
                  response: Response = None) -> Response:
    """删除监控对象（联动注销监听）。"""
    target = db.get_target(target_id)
    if not target:
        raise HTTPException(status_code=404, detail="监控对象不存在")
    db.delete_target(target_id)
    svc.unregister_target(target["username"])
    logger.info("删除监控对象：%s", target["username"])
    return Response(status_code=204)
