# -*- coding: utf-8 -*-
"""商机路由：列表 / 详情（含上下文、清除未读）/ 状态 / 回复。

路由（设计文档 3.2）：
- GET   /api/opportunities?page=&page_size=20&session=&type_id=&status=&keyword=
- GET   /api/opportunities/{id}          详情（并清除该条未读）
- PATCH /api/opportunities/{id}          {status}
- POST  /api/opportunities/{id}/reply    {text, at?, quote?} → 202 受理
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_db, get_notifier, get_sender, get_wechat_service
from app.config import CONTEXT_COUNT
from app.db import Database
from app.schemas import (
    MessageOut,
    OpportunityDetailOut,
    OpportunityListOut,
    OpportunityListItem,
    OpportunityStatusUpdate,
    ReplyAccepted,
    ReplyRequest,
)
from app.sender import SendTask
from app.wechat_service import WeChatService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/opportunities", tags=["opportunities"])


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------
@router.get("", response_model=OpportunityListOut)
def list_opportunities(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: str = "",
    type_id: Optional[int] = Query(default=None, ge=1),
    status: str = Query(default="", pattern="^(|pending|replied|ignored)$"),
    keyword: str = "",
    db: Database = Depends(get_db),
) -> OpportunityListOut:
    """商机列表（时间倒序；status 为空时排除已忽略项）。"""
    total, rows = db.list_opportunities(
        page=page, page_size=page_size, session=session,
        type_id=type_id, status=status, keyword=keyword)
    items = [OpportunityListItem(**r) for r in rows]
    return OpportunityListOut(total=total, items=items)


# ---------------------------------------------------------------------------
# 详情
# ---------------------------------------------------------------------------
def _load_detail(opp_id: int, db: Database, svc: WeChatService
                 ) -> OpportunityDetailOut:
    """读取商机详情 + 上下文（前后各 N 条，复用历史消息查询）。"""
    opp = db.get_opportunity(opp_id)
    if not opp:
        raise HTTPException(status_code=404, detail="商机不存在")
    context: List[MessageOut] = []
    msgs = svc.get_messages(opp["session_username"],
                            limit=CONTEXT_COUNT * 2 + 1)
    for m in msgs:
        context.append(MessageOut(**m))
    return OpportunityDetailOut(
        id=opp["id"],
        session_username=opp["session_username"],
        session_name=opp["session_name"],
        sender_wxid=opp["sender_wxid"],
        sender_name=opp["sender_name"],
        window_start=opp["window_start"],
        window_end=opp["window_end"],
        merged_count=opp["merged_count"],
        raw_text=opp["raw_text"],
        type_id=opp["type_id"],
        type_name=opp["type_name"],
        summary=opp["summary"],
        key_quote=opp["key_quote"],
        confidence=opp["confidence"],
        status=opp["status"],
        reply_content=opp["reply_content"],
        replied_at=opp["replied_at"],
        created_at=opp["created_at"],
        context=context,
    )


@router.get("/{opp_id}", response_model=OpportunityDetailOut)
def get_opportunity(opp_id: int, db: Database = Depends(get_db),
                    svc: WeChatService = Depends(get_wechat_service),
                    ) -> OpportunityDetailOut:
    """商机详情（并清除该条未读，广播 unread_cleared）。"""
    detail = _load_detail(opp_id, db, svc)
    get_notifier().clear_unread(opp_id)
    return detail


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
@router.patch("/{opp_id}", response_model=OpportunityListItem)
def update_opportunity_status(opp_id: int, body: OpportunityStatusUpdate,
                              db: Database = Depends(get_db),
                              ) -> OpportunityListItem:
    """标记商机状态（待处理/已回复/已忽略，误报处理 P0-9）。"""
    opp = db.get_opportunity(opp_id)
    if not opp:
        raise HTTPException(status_code=404, detail="商机不存在")
    if db.update_opportunity_status(opp_id, body.status) <= 0:
        raise HTTPException(status_code=500, detail="状态更新失败")
    get_notifier().broadcast("opportunity_update",
                             {"opportunity_id": opp_id, "status": body.status})
    opp = db.get_opportunity(opp_id) or {}
    return OpportunityListItem(**opp)


# ---------------------------------------------------------------------------
# 回复
# ---------------------------------------------------------------------------
@router.post("/{opp_id}/reply", response_model=ReplyAccepted, status_code=202)
def reply_opportunity(opp_id: int, body: ReplyRequest,
                      db: Database = Depends(get_db),
                      svc: WeChatService = Depends(get_wechat_service),
                      ) -> ReplyAccepted:
    """人工回复：入队 GUI 串行发送，结果经 WS ``send_result`` 事件送达。

    目标校验①：会话须存在于监控对象或微信联系人库。
    """
    opp = db.get_opportunity(opp_id)
    if not opp:
        raise HTTPException(status_code=404, detail="商机不存在")
    session_username = opp["session_username"]
    # 目标校验①：监控对象或联系人存在性
    if not db.get_target_by_username(session_username) \
            and not svc.session_exists(session_username):
        raise HTTPException(
            status_code=400,
            detail=f"目标会话不存在或已失效：{opp['session_name']}，"
                   "请确认微信仍在使用该会话")
    task = SendTask(
        opportunity_id=opp_id,
        session_username=session_username,
        session_name=opp["session_name"] or opp["session_username"],
        text=body.text.strip(),
        at=[a.strip() for a in body.at if a.strip()],
        quote_local_id=body.quote.local_id if body.quote else None,
    )
    if not task.text:
        raise HTTPException(status_code=422, detail="回复内容不能为空")
    if not get_sender().submit(task):
        raise HTTPException(status_code=500, detail="发送队列不可用，请稍后重试")
    logger.info("回复任务已受理（商机 #%d → %s）", opp_id, task.session_name)
    return ReplyAccepted(ok=True, message="已加入发送队列，发送结果将在页面提示")
