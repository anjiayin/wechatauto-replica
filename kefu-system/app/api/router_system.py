# -*- coding: utf-8 -*-
"""运行状态与会话/消息查询路由。

路由（设计文档 3.2）：
- GET /api/status
- GET /api/sessions/search?keyword=&limit=20
- GET /api/messages/{username}?before_seq=&limit=50
- WS  /ws/events
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from app.api.deps import (
    get_db,
    get_notifier,
    get_pipeline,
    get_wechat_service,
)
from app.db import Database
from app.schemas import (
    MessageOut,
    QueueStatus,
    SessionItemOut,
    StatusOut,
    WechatStatus,
    WorkerStatus,
)
from app.pipeline import Pipeline
from app.wechat_service import WeChatService

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /api/status
# ---------------------------------------------------------------------------
@router.get("/api/status", response_model=StatusOut)
def get_status(db: Database = Depends(get_db),
               svc: WeChatService = Depends(get_wechat_service),
               pipeline: Pipeline = Depends(get_pipeline)) -> StatusOut:
    """运行状态（顶部横幅数据源）。"""
    notifier = get_notifier()
    ai_cfg_base = db.get_setting("ai.base_url", "")
    ai_cfg_model = db.get_setting("ai.model", "")
    ai_key_enc = db.get_setting("ai.api_key_enc", "")
    ai_configured = bool(ai_cfg_base and ai_cfg_model and ai_key_enc)
    return StatusOut(
        listener_running=svc.listener_running,
        monitored_count=svc.monitored_count,
        wechat=WechatStatus(alive=svc.available,
                            visible=svc.probe_wechat_window()),
        queue=QueueStatus(size=pipeline.queue_size, max=pipeline.queue_max,
                          dropped_total=pipeline.dropped_total),
        worker=WorkerStatus(busy=pipeline.busy, max=2),
        ai_configured=ai_configured,
        watermark_saved_at=svc.watermark_saved_at,
        degraded_reason=svc.degraded_reason,
        unread_count=notifier.unread_count,
    )


# ---------------------------------------------------------------------------
# GET /api/sessions/search
# ---------------------------------------------------------------------------
@router.get("/api/sessions/search", response_model=List[SessionItemOut])
def search_sessions(
    keyword: str = "",
    limit: int = Query(default=20, ge=1, le=100),
    svc: WeChatService = Depends(get_wechat_service),
) -> List[SessionItemOut]:
    """会话搜索（群 + 好友，复用 search_contact/get_sessions）。"""
    items = svc.search_sessions(keyword=keyword, limit=limit)
    return [SessionItemOut(**item) for item in items]


# ---------------------------------------------------------------------------
# GET /api/messages/{username}
# ---------------------------------------------------------------------------
@router.get("/api/messages/{username}", response_model=List[MessageOut])
def get_messages(username: str,
                 before_seq: int = Query(default=0, ge=0),
                 limit: int = Query(default=50, ge=1, le=200),
                 svc: WeChatService = Depends(get_wechat_service)
                 ) -> List[MessageOut]:
    """会话历史消息（详情上下文 / 消息浏览页复用）。"""
    msgs = svc.get_messages(username, limit=limit, before_seq=before_seq)
    return [MessageOut(**m) for m in msgs]


# ---------------------------------------------------------------------------
# WS /ws/events
# ---------------------------------------------------------------------------
@router.websocket("/ws/events")
async def ws_events(ws: WebSocket) -> None:
    """事件推送（新商机 / 状态变化 / 未读清零 / 发送结果）。"""
    notifier = get_notifier()
    await notifier.connect(ws)
    try:
        while True:
            # 客户端仅接收；收到的消息（含断开探测）直接忽略
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover
        logger.debug("WS 连接异常断开：%s", exc)
    finally:
        notifier.disconnect(ws)
