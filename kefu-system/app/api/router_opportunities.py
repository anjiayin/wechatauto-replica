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

import json
import logging
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import (get_db, get_notifier, get_sender,
                          get_wechat_service, get_wechat_service_optional)
from app.config import CONTEXT_COUNT
from app.db import Database
from app.schemas import (
    GroupMemberOut,
    GroupMembersOut,
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


def _parse_raw_refs(raw: Any) -> List[dict]:
    """把库里的 ``raw_refs`` JSON 字符串解析成引用列表（批次 11 需求 1）。

    存量商机该列为 ``'[]'``，手改库/异常写入也可能是空串、NULL 或坏 JSON；
    这里一律容错成空列表——详情页只是少显示几张图，绝不能 500。

    Args:
        raw: DB 取出的原始值（通常是 JSON 字符串）。

    Returns:
        合法引用字典列表（非 dict 元素被丢弃）。
    """
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        logger.warning("商机 raw_refs 不是合法 JSON（已按空处理）：%s", text[:120])
        return []
    if not isinstance(parsed, list):
        logger.warning("商机 raw_refs 不是数组（已按空处理）：%s", text[:120])
        return []
    return [x for x in parsed if isinstance(x, dict)]


def _is_openim(username: Any) -> bool:
    """会话是否企业微信/客服账号（@openim / @kefu）。"""
    u = str(username or "").strip().lower()
    return u.endswith("@openim") or "@kefu" in u


def _display_with_corp(svc: Any, username: str, stored: str) -> str:
    """把「只存了昵称」的企业微信显示名补成「昵称@企业简称」。

    存量商机的 ``session_name`` 是监听当时用 ``get_display_name`` 写的，
    企业微信联系人只有昵称（与微信界面上的「安佳胤@我的车车」不一致）。
    这里按 username 现算一次带企业名的显示名；任何一步失败都原样返回
    已存值——列表页绝不能因为补个名字就报错或变慢（企业简称表在服务内
    已整表缓存 10 分钟）。

    Args:
        svc: 微信服务（可能为 None，或测试替身缺方法）。
        username: 会话/发送人 username。
        stored: 库里已存的显示名。

    Returns:
        展示用显示名。
    """
    name = str(stored or "")
    if not name or not _is_openim(username) or "@" in name:
        return name
    resolver = getattr(svc, "display_name_with_corp", None)
    if not callable(resolver):
        return name
    try:
        got = str(resolver(str(username)) or "").strip()
    except Exception:      # 显示名是增强项：失败就用已存名字
        logger.debug("补齐企业名失败（沿用已存显示名）：%s", username)
        return name
    return got or name


def _with_displays(items: List[dict], svc: Any) -> List[dict]:
    """给一批商机行补 ``session_display`` / ``sender_display``（就地返回拷贝）。"""
    out: List[dict] = []
    for row in items:
        item = dict(row)
        session_username = str(item.get("session_username") or "")
        item["session_display"] = _display_with_corp(
            svc, session_username, item.get("session_name", ""))
        sender_wxid = str(item.get("sender_wxid") or "")
        sender = str(item.get("sender_name") or "")
        # 私聊（含企业微信）的发送人就是会话对端：直接复用补好的来源名，
        # 免得同一个名字在「来源」「发送人」两处显示不一致
        if sender_wxid and sender_wxid == session_username:
            item["sender_display"] = item["session_display"]
        else:
            item["sender_display"] = _display_with_corp(
                svc, sender_wxid, sender)
        out.append(item)
    return out


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
    """商机列表（时间倒序；status 为空时排除已忽略项；direction 买卖货筛选）。

    每行附加 ``unread`` 未读标识（内存态，供列表页行内展示 + 点击消除），
    以及 ``session_display`` / ``sender_display``（企业微信显示「昵称@企业名」，
    见 :func:`_display_with_corp`）。
    """
    total, rows = db.list_opportunities(
        page=page, page_size=page_size, session=session,
        type_id=type_id, status=status, keyword=keyword, direction=direction)
    unread_ids = get_notifier().unread_ids()
    svc = get_wechat_service_optional()
    items = [OpportunityListItem(**r, unread=r["id"] in unread_ids)
             for r in _with_displays(rows, svc)]
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
                    svc: WeChatService = Depends(get_wechat_service),
                    ) -> OpportunityDetailOut:
    """商机详情（并清除该条未读，广播 unread_cleared）。

    体验优化：详情与上下文拆分——本接口只读业务 SQLite（毫秒级），
    上下文消息由 GET /{opp_id}/context 异步加载，微信库繁忙时详情照常秒开。

    ``raw_text_display``：群消息原文每行的发送者 ID 换成显示名（发送人昵称
    已由 wechat_service 解析，失败时原样返回，绝不影响详情加载速度）。

    ``raw_refs``：原文行里的图片引用（解析失败容错为空列表），前端据此把
    对应的原文行渲染成真图（批次 11 需求 1）。
    """
    opp = db.get_opportunity(opp_id)
    if not opp:
        raise HTTPException(status_code=404, detail="商机不存在")
    get_notifier().clear_unread(opp_id)
    try:
        _session, raw_display = svc.build_log_displays(
            str(opp.get("session_username") or ""),
            str(opp.get("raw_text") or ""))
    except Exception:  # 显示名解析失败不影响详情
        logger.exception("商机 #%d 原文显示名解析失败（回退原始文本）", opp_id)
        raw_display = str(opp.get("raw_text") or "")
    # raw_refs 在库里是 JSON 字符串，要解析成数组交给前端（解析失败容错为 []）
    detail = dict(opp)
    detail["raw_refs"] = _parse_raw_refs(opp.get("raw_refs"))
    # 企业微信显示名补齐（存量商机只存了昵称）
    detail = _with_displays([detail], svc)[0]
    return OpportunityDetailOut(
        **detail, unread=False, raw_text_display=raw_display)


# ---------------------------------------------------------------------------
# 单条已读（反馈 9：列表行内「未读」标识点击即消失，无需等详情加载）
# ---------------------------------------------------------------------------
@router.post("/{opp_id}/read")
def mark_opportunity_read(opp_id: int, db: Database = Depends(get_db)) -> dict:
    """把单条商机标记为已读（幂等；广播 unread_cleared 让其它页面同步）。"""
    if not db.get_opportunity(opp_id):
        raise HTTPException(status_code=404, detail="商机不存在")
    cleared = get_notifier().clear_unread(opp_id)
    return {"ok": True, "cleared": cleared}


# ---------------------------------------------------------------------------
# @ 群成员候选（前端下拉选择，避免手输昵称 @ 不到人）
# ---------------------------------------------------------------------------
@router.get("/{opp_id}/members", response_model=GroupMembersOut)
def list_group_members(opp_id: int,
                       db: Database = Depends(get_db),
                       svc: WeChatService = Depends(get_wechat_service),
                       ) -> GroupMembersOut:
    """该商机会话的群成员列表（非群会话 is_group=False）。

    前端用群内显示名（= 微信 @ 弹层里显示的名字）作为 @ 值；读取失败
    返回 error，前端降级为手输。
    """
    opp = db.get_opportunity(opp_id)
    if not opp:
        raise HTTPException(status_code=404, detail="商机不存在")
    username = opp["session_username"] or ""
    if not username.endswith("@chatroom"):
        return GroupMembersOut(is_group=False)
    try:
        members = svc.list_group_members(username)
    except Exception as exc:
        logger.warning("读取群成员失败（%s）：%s", username, exc)
        return GroupMembersOut(is_group=True,
                               error=f"读取群成员失败：{exc}")
    return GroupMembersOut(
        is_group=True,
        items=[GroupMemberOut(**m) for m in members],
    )


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

    ``manual_confirm=True``（默认）时只把内容填进微信输入框、不按发送键：
    同名联系人与企业微信身份无法百分百确定收件人的场景下，把最后一步交给
    用户在微信里核对，避免发错人（商机状态保持待处理，不置「已回复」）。
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
    # 会话显示名：企业微信联系人为「昵称@企业简称」（展开发送时会剥离后缀再搜索）
    session_name = opp["session_name"] or opp["session_username"]
    if _is_openim(session_username) and "@" not in session_name:
        session_name = _display_with_corp(svc, session_username, session_name)
    extra_note = ""
    manual = bool(body.manual_confirm)
    if manual and (body.at or body.quote is not None):
        # 人工确认模式下只做「清空输入框 + 填入正文」，@ 弹层与引用卡片都
        # 需要发送动作配合才能成型。丢弃它们比强行自动发送安全：这一步本来
        # 就是让用户在微信里自己收尾，@ 与引用在微信里点一下即可。
        extra_note = "（人工确认模式未包含 @成员/引用，请在微信里自行补上）"
        logger.info("人工确认模式忽略 @/引用（商机 #%d）", opp_id)
    task = SendTask(
        opportunity_id=opp_id,
        session_username=session_username,
        session_name=session_name,
        text=body.text.strip(),
        at=[] if manual else [a.strip() for a in body.at if a.strip()],
        quote_local_id=None if manual else (
            body.quote.local_id if body.quote else None),
        manual_confirm=manual,
    )
    if not task.text:
        raise HTTPException(status_code=422, detail="回复内容不能为空")
    if not get_sender().submit(task):
        raise HTTPException(status_code=500, detail="发送队列不可用，请稍后重试")
    action = "填入输入框" if task.manual_confirm else "自动发送"
    logger.info("回复任务已受理（商机 #%d → %s，%s）", opp_id, task.session_name,
                action)
    return ReplyAccepted(
        ok=True,
        message=(f"已加入队列：将把内容填入「{session_name}」的微信输入框，"
                 f"请在微信里核对后点发送{extra_note}" if task.manual_confirm
                 else "已加入发送队列，发送结果将在页面提示"))
