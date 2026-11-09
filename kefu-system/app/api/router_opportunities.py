# -*- coding: utf-8 -*-
"""商机路由：列表 / 详情（快速，不查微信库）/ 上下文（独立接口）/ 状态 / 回复。

路由（设计文档 3.2）：
- GET   /api/opportunities?page=&page_size=20&session=&type_id=&status=&keyword=
- GET   /api/opportunities/{id}                详情（并清除该条未读；不查微信库，秒开）
- GET   /api/opportunities/{id}/context        上下文消息（独立异步，失败可重试）
- PATCH /api/opportunities/{id}                {status}
- POST  /api/opportunities/{id}/reply          {text, at?, quote?} → 202 受理
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
    OpportunityContextOut,
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
    status: str = Query(default="",
                        pattern="^(|pending|replied|ignored|handled)$"),
    keyword: str = "",
    direction: str = Query(default="", pattern="^(|buy|sell)$"),
    db: Database = Depends(get_db),
) -> OpportunityListOut:
    """商机列表（时间倒序；status 为空时排除已忽略项；direction 买卖货筛选）。"""
    total, rows = db.list_opportunities(
        page=page, page_size=page_size, session=session,
        type_id=type_id, status=status, keyword=keyword, direction=direction)
    items = [OpportunityListItem(**r) for r in rows]
    return OpportunityListOut(total=total, items=items)


# ---------------------------------------------------------------------------
# 全部已读（反馈 12：一键清零未读角标）
# ---------------------------------------------------------------------------
@router.post("/read-all")
def read_all_opportunities(
    notifier=Depends(get_notifier),
) -> dict:
    """全部已读：清空未读角标并广播（不影响商机状态本身）。"""
    cleared = notifier.clear_all_unread()
    return {"cleared": cleared}


# ---------------------------------------------------------------------------
# 详情（快速：只读业务库，不查微信库；上下文走独立接口）
# ---------------------------------------------------------------------------
@router.get("/{opp_id}", response_model=OpportunityDetailOut)
def get_opportunity(opp_id: int, db: Database = Depends(get_db),
                    ) -> OpportunityDetailOut:
    """商机详情（并清除该条未读，广播 unread_cleared）。

    体验优化：详情与上下文拆分——本接口只读业务 SQLite（毫秒级），
    上下文消息由 GET /{opp_id}/context 异步加载，微信库繁忙时详情照常秒开。
    """
    opp = db.get_opportunity(opp_id)
    if not opp:
        raise HTTPException(status_code=404, detail="商机不存在")
    get_notifier().clear_unread(opp_id)
    return OpportunityDetailOut(**opp)


# ---------------------------------------------------------------------------
# 上下文（独立接口：微信库查询 + 瞬时故障重试，见 WeChatService）
# ---------------------------------------------------------------------------
@router.get("/{opp_id}/context", response_model=OpportunityContextOut)
def get_opportunity_context(opp_id: int,
                            db: Database = Depends(get_db),
                            svc: WeChatService = Depends(get_wechat_service),
                            ) -> OpportunityContextOut:
    """商机上下文消息（前后各 N 条，时间倒序返回，前端转正序渲染）。

    微信客户端写库期间的瞬时 malformed/空结果由服务层自动重试（最多 2 次）；
    仍失败时 error 返回用户可读的中文原因，前端展示 + 重新加载按钮。
    """
    opp = db.get_opportunity(opp_id)
    if not opp:
        raise HTTPException(status_code=404, detail="商机不存在")
    msgs, error = svc.get_messages_with_error(
        opp["session_username"], limit=CONTEXT_COUNT * 2 + 1,
        retry_on_empty=True)
    return OpportunityContextOut(
        items=[MessageOut(**m) for m in msgs], error=error)


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
@router.patch("/{opp_id}", response_model=OpportunityListItem)
def update_opportunity_status(opp_id: int, body: OpportunityStatusUpdate,
                              db: Database = Depends(get_db),
                              ) -> OpportunityListItem:
    """标记商机状态（待处理/已回复/已忽略/已自行处理，误报处理 P0-9）。"""
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
