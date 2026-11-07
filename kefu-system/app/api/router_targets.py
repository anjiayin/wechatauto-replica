# -*- coding: utf-8 -*-
"""监控对象 CRUD 路由。

路由（设计文档 3.2）：
- GET    /api/monitor-targets
- POST   /api/monitor-targets           {username, display_name?} → 目标对象
- PATCH  /api/monitor-targets/{id}      {enabled}
- DELETE /api/monitor-targets/{id}      → 204

体验优化说明：
- POST 落库后立即返回 201，Listener 注册放后台线程执行（响应快）；
- 配额上限从 settings 键 ``monitor.max_targets`` 读取（默认 200 兜底）；
- 列表读取时对陈旧显示名快照（display_name == username）做一次性补齐。
"""
from __future__ import annotations

import logging
import sqlite3
import threading

from fastapi import APIRouter, Depends, HTTPException, Response

from app.api.deps import get_db, get_wechat_service
from app.db import Database, read_monitor_max_targets
from app.schemas import MonitorTargetCreate, MonitorTargetOut, MonitorTargetUpdate
from app.wechat_service import WeChatService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/monitor-targets", tags=["monitor-targets"])


def _register_async(svc: WeChatService, username: str) -> None:
    """后台线程注册监听：API 立即返回，注册失败只记日志（不影响落库结果）。"""

    def _run() -> None:
        try:
            svc.register_target(username)
        except Exception:
            logger.exception("后台注册监听失败：%s", username)

    threading.Thread(target=_run, name=f"kefu-register-{username[:24]}",
                     daemon=True).start()


@router.get("", response_model=list[MonitorTargetOut])
def list_targets(db: Database = Depends(get_db),
                 svc: WeChatService = Depends(get_wechat_service)
                 ) -> list[MonitorTargetOut]:
    """已配置监控对象列表（补齐陈旧显示名快照：右侧列表优先显示群名/昵称）。"""
    targets = db.list_targets()
    for t in targets:
        if t["display_name"] != t["username"]:
            continue
        try:
            fresh = svc.get_display_name(t["username"]) or ""
        except Exception:
            fresh = ""
        if fresh and fresh != t["username"]:
            db.update_target_display_name(t["id"], fresh)
            t["display_name"] = fresh
    return [MonitorTargetOut(**t) for t in targets]


@router.post("", response_model=MonitorTargetOut, status_code=201)
def create_target(body: MonitorTargetCreate,
                  db: Database = Depends(get_db),
                  svc: WeChatService = Depends(get_wechat_service)
                  ) -> MonitorTargetOut:
    """新增监控对象（立即落库返回，监听注册后台执行）。

    显示名优先用前端候选列表传入的快照；缺失时由微信联系人库解析。
    kind 由 username 形态判定（@chatroom = 群）。
    """
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="会话 username 不能为空")
    if db.get_target_by_username(username):
        raise HTTPException(status_code=400, detail="该会话已在监控列表中")
    kind = "group" if username.endswith("@chatroom") else "friend"
    display_name = (body.display_name.strip()
                    or svc.get_display_name(username) or username)
    # 配额上限从 settings 读取（monitor.max_targets，无键时用常量兜底）
    max_targets = read_monitor_max_targets(db)
    # 原子「配额校验 + 插入」（BUG-3 修复：check-then-insert 并发越界）
    try:
        target = db.insert_target_within_limit(username, display_name, kind,
                                               max_targets)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="该会话已在监控列表中")
    if target is None:
        raise HTTPException(
            status_code=400,
            detail=f"监控对象已达上限 {max_targets} 个，请先移除部分再添加")
    if target["enabled"]:
        _register_async(svc, username)
    logger.info("新增监控对象：%s（%s）", display_name, username)
    return MonitorTargetOut(**target)


@router.patch("/{target_id}", response_model=MonitorTargetOut)
def update_target(target_id: int, body: MonitorTargetUpdate,
                  db: Database = Depends(get_db),
                  svc: WeChatService = Depends(get_wechat_service)
                  ) -> MonitorTargetOut:
    """启用/停用监控对象（启用时后台注册 Listener 回调，停用即时注销）。"""
    target = db.get_target(target_id)
    if not target:
        raise HTTPException(status_code=404, detail="监控对象不存在")
    db.update_target_enabled(target_id, body.enabled)
    if body.enabled:
        _register_async(svc, target["username"])
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
