# -*- coding: utf-8 -*-
"""分析日志路由：分页筛选查询（漏判修正页数据源）。

路由：
- GET /api/analysis-logs?status=&session=&sender=&keyword=&page=&page_size=

每条返回附加派生展示字段（不改 DB 存储）：
- session_display：来源会话显示名（备注/昵称优先，失败回退原始 ID）；
- sender_display：发送人显示名（群昵称 > 联系人昵称 > wxid，批次 11 需求 2）；
- input_text_display：消息内容展示版（群消息发送者前缀已解析为显示名）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_db, get_wechat_service
from app.db import Database
from app.schemas import AnalysisLogListOut, AnalysisLogOut
from app.sender_display import (resolve_sender_display,
                                resolve_wechat_id as _alias_of)
from app.wechat_service import WeChatService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analysis-logs", tags=["analysis-logs"])

# 会话名 / 发送人筛选的候选上限：逐条解析显示名要走联系人/群查询，必须有
# 上限保护，否则候选很多时会拖垮请求（批次 6 需求 2 / 批次 11 需求 2）。
SESSION_CANDIDATE_LIMIT = 500
SENDER_CANDIDATE_LIMIT = 500


def _name_resolver(svc: WeChatService):
    """取联系人昵称解析函数（测试替身可能只实现部分方法，取不到就退化）。"""
    resolver = getattr(svc, "get_display_name", None)
    return resolver if callable(resolver) else None


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
        # 微信号（alias）同样可搜——界面上展示的就是它，用户自然按它找人
        alias = _alias_of(svc, uid).lower()
        if key in uid.lower() or key in display.lower() or key in alias:
            hits.append(uid)
    return hits


def _match_log_senders(db: Database, svc: WeChatService,
                       keyword: str) -> list[str]:
    """把用户输入的发送人「昵称 / wxid 片段」解析成 ``sender_wxid`` 候选列表。

    日志表只存 ``sender_wxid``，页面上展示的是解析后的显示名（群昵称最准，
    而群昵称按「群 + 成员」记录），所以候选集要带会话 username 才能算出与
    微信一致的名字。

    匹配规则（大小写不敏感）：用户输入是显示名或 wxid 的子串即命中。
    单条解析失败只跳过该条（不能让整页 500），候选数有上限。

    Args:
        db: 数据库实例（提供 ``distinct_log_sender_pairs``）。
        svc: 微信服务实例（提供 ``get_display_name``）。
        keyword: 用户输入的发送人昵称或微信 ID 片段。

    Returns:
        命中的 ``sender_wxid`` 列表（去重、未命中返回空列表）。
    """
    key = str(keyword or "").strip().lower()
    if not key:
        return []
    try:
        pairs = db.distinct_log_sender_pairs(limit=SENDER_CANDIDATE_LIMIT)
    except Exception:
        logger.exception("查询日志发送人候选失败（退化为 wxid 模糊匹配）")
        return []
    resolver = _name_resolver(svc)
    hits: list[str] = []
    for wxid, chatroom in pairs[:SENDER_CANDIDATE_LIMIT]:
        uid = str(wxid or "")
        if not uid or uid in hits:
            continue
        try:
            display = resolve_sender_display(db, resolver, chatroom, uid)
        except Exception:  # pragma: no cover - resolve 内部已兜底，双保险
            logger.warning("解析发送人显示名失败（跳过该条）：%s", uid)
            continue
        if key in uid.lower() or key in display.lower() \
                or key in _alias_of(svc, uid).lower():
            hits.append(uid)
    return hits


def _sender_displays(db: Database, svc: WeChatService,
                     rows: list[dict]) -> list[str]:
    """按要求把一页日志的发送人显示名算出来（单条失败回退 wxid，不抛）。"""
    resolver = _name_resolver(svc)
    out: list[str] = []
    for row in rows:
        wxid = str(row.get("sender_wxid") or "")
        if not wxid:
            out.append("")
            continue
        try:
            out.append(resolve_sender_display(
                db, resolver, str(row.get("session_username") or ""), wxid))
        except Exception:  # pragma: no cover - resolve 内部已兜底，双保险
            logger.warning("解析发送人显示名失败（回退 wxid）：%s", wxid)
            out.append(wxid)
    return out


@router.get("", response_model=AnalysisLogListOut)
def list_analysis_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status: str = Query(default="",
                        pattern="^(|ok|skipped|error|below_threshold)$"),
    session: str = "",
    sender: str = "",
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
    - sender：发送人筛选，**支持昵称/群昵称/wxid 片段**（批次 11 需求 2），
      解析方式与会话一致（显示名 → wxid 候选 + 原 LIKE 路径兜底）；
    - keyword：消息内容筛选（input_text 模糊匹配）。
    """
    session_q = session.strip()
    session_in: list[str] = []
    if session_q:
        session_in = _match_log_sessions(db, svc, session_q)
    sender_q = sender.strip()
    sender_in: list[str] = []
    if sender_q:
        sender_in = _match_log_senders(db, svc, sender_q)
    total, rows = db.list_analysis_logs(
        page=page, page_size=page_size, status=status,
        session=session_q, keyword=keyword.strip(),
        session_in=session_in, sender=sender_q, sender_in=sender_in)
    displays: list[str] = _sender_displays(db, svc, rows)
    items: list[AnalysisLogOut] = []
    for row, sender_display in zip(rows, displays):
        session_display, text_display = svc.build_log_displays(
            str(row.get("session_username") or ""),
            str(row.get("input_text") or ""))
        items.append(AnalysisLogOut(
            **row, session_display=session_display,
            session_alias=_alias_of(
                svc, str(row.get("session_username") or "")),
            sender_alias=_alias_of(svc, str(row.get("sender_wxid") or "")),
            input_text_display=text_display,
            sender_display=sender_display))
    return AnalysisLogListOut(total=total, items=items)
