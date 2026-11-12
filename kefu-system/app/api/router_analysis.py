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

# 会话名筛选的候选上限：逐条解析显示名要走联系人/群查询，必须有上限保护，
# 否则会话很多时会拖垮请求（批次 6 需求 2）。
SESSION_CANDIDATE_LIMIT = 500


def _match_log_sessions(db: Database, svc: WeChatService,
                        keyword: str) -> list[str]:
    """把用户输入的会话名/备注/群名解析成 ``session_username`` 候选列表。

    匹配规则（大小写不敏感）：用户输入是显示名或 username 的子串即命中。
    单条显示名解析失败只跳过该条（不能让整页 500），整体候选数有上限。

    Args:
        db: 数据库实例（提供 ``distinct_log_sessions``）。
        svc: 微信服务实例（提供 ``get_display_name``）。
        keyword: 用户输入的会话名或微信 ID 片段。

    Returns:
        命中的 ``session_username`` 列表（未命中返回空列表）。
    """
    key = str(keyword or "").strip().lower()
    if not key:
        return []
    try:
        candidates = db.distinct_log_sessions(limit=SESSION_CANDIDATE_LIMIT)
    except Exception:
        logger.exception("查询日志会话候选失败（退化为 username 模糊匹配）")
        return []
    hits: list[str] = []
    for username in candidates[:SESSION_CANDIDATE_LIMIT]:
        uid = str(username or "")
        if not uid:
            continue
        try:
            display = str(svc.get_display_name(uid) or "")
        except Exception:
            logger.warning("解析会话显示名失败（跳过该条）：%s", uid)
            continue
        if key in uid.lower() or key in display.lower():
            hits.append(uid)
    return hits


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

    - session：来源会话筛选，**支持会话名/备注/群名**（批次 6 需求 2）：
      页面上「来源会话」列展示的是解析后的显示名，而库里存的是
      ``session_username``，因此先用候选集把「显示名 → username」解析出来，
      再把命中的 username 列表交给 DB 精确匹配；同时保留原 LIKE 路径，
      保证直接贴 username 片段仍能命中；
    - keyword：消息内容筛选（input_text 模糊匹配）。
    """
    session_q = session.strip()
    session_in: list[str] = []
    if session_q:
        session_in = _match_log_sessions(db, svc, session_q)
    total, rows = db.list_analysis_logs(
        page=page, page_size=page_size, status=status,
        session=session_q, keyword=keyword.strip(),
        session_in=session_in)
    items: list[AnalysisLogOut] = []
    for row in rows:
        session_display, text_display = svc.build_log_displays(
            str(row.get("session_username") or ""),
            str(row.get("input_text") or ""))
        items.append(AnalysisLogOut(
            **row, session_display=session_display,
            input_text_display=text_display))
    return AnalysisLogListOut(total=total, items=items)
