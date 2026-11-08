# -*- coding: utf-8 -*-
"""分析日志路由：分页筛选查询（漏判修正页数据源）。

路由：
- GET /api/analysis-logs?status=&session=&keyword=&page=&page_size=

每条返回附加派生展示字段（不改 DB 存储）：
- session_display：来源会话显示名（备注/昵称优先，失败回退原始 ID）；
- input_text_display：消息内容展示版（群消息发送者前缀已解析为显示名）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_db, get_wechat_service
from app.db import Database
from app.schemas import AnalysisLogListOut, AnalysisLogOut
from app.wechat_service import WeChatService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analysis-logs", tags=["analysis-logs"])


@router.get("", response_model=AnalysisLogListOut)
def list_analysis_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status: str = Query(default="",
                        pattern="^(|ok|skipped|error|below_threshold)$"),
    session: str = "",
    keyword: str = "",
    db: Database = Depends(get_db),
    svc: WeChatService = Depends(get_wechat_service),
) -> AnalysisLogListOut:
    """分析日志分页列表（id 倒序；status 空为全部）。

    - session：来源会话筛选（session_username 模糊匹配）；
    - keyword：消息内容筛选（input_text 模糊匹配）。
    """
    total, rows = db.list_analysis_logs(
        page=page, page_size=page_size, status=status,
        session=session.strip(), keyword=keyword.strip())
    items: list[AnalysisLogOut] = []
    for row in rows:
        session_display, text_display = svc.build_log_displays(
            str(row.get("session_username") or ""),
            str(row.get("input_text") or ""))
        items.append(AnalysisLogOut(
            **row, session_display=session_display,
            input_text_display=text_display))
    return AnalysisLogListOut(total=total, items=items)
