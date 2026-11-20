# -*- coding: utf-8 -*-
"""群消息总结 REST 路由（薄路由：校验 → 调服务 → 异常翻译）。

错误口径（⑦-6）：400 参数/无消息、404 不存在、413 超预算、502 AI 失败、
503 微信库不可读，detail 一律中文。不提供任何发送回群端点。
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from app import analyzer
from app.api.deps import get_db, get_summary_service
from app.config import (
    BUDGET_MODE_VALUES,
    KEY_SUMMARY_BUDGET_MONEY,
    KEY_SUMMARY_BUDGET_MODE,
    KEY_SUMMARY_BUDGET_TOKENS,
    KEY_SUMMARY_OVER_LIMIT_ACTION,
    KEY_SUMMARY_SCHEDULE_CONFIGS,
    KEY_SUMMARY_SCHEDULE_ENABLED,
    KEY_SUMMARY_TRUNCATE_RECENT_N,
    OVER_LIMIT_ACTION_VALUES,
)
from app.db import Database
from app.schemas import (
    STYLE_VALUES,
    SummaryGenerateIn,
    SummaryListOut,
    SummaryMembersOut,
    SummaryOut,
    SummarySettingsOut,
    SummarySettingsUpdate,
)
from app.summary import SummaryOverBudgetError, SummaryService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/summary", tags=["summary"])

# 设置页配置区可编辑键（白名单，防任意 settings 写入）
# 预算 5 键 + 定时 2 键（P0 仅保存配置，SummaryScheduler 不启动线程）。
_SETTINGS_KEYS = (
    KEY_SUMMARY_BUDGET_MODE,
    KEY_SUMMARY_BUDGET_MONEY,
    KEY_SUMMARY_BUDGET_TOKENS,
    KEY_SUMMARY_OVER_LIMIT_ACTION,
    KEY_SUMMARY_TRUNCATE_RECENT_N,
    KEY_SUMMARY_SCHEDULE_ENABLED,
    KEY_SUMMARY_SCHEDULE_CONFIGS,
)
_SETTINGS_DEFAULTS = {
    KEY_SUMMARY_BUDGET_MODE: "money",
    KEY_SUMMARY_BUDGET_MONEY: "1.0",
    KEY_SUMMARY_BUDGET_TOKENS: "60000",
    KEY_SUMMARY_OVER_LIMIT_ACTION: "block",
    KEY_SUMMARY_TRUNCATE_RECENT_N: "200",
    KEY_SUMMARY_SCHEDULE_ENABLED: "0",
    KEY_SUMMARY_SCHEDULE_CONFIGS: "[]",
}


def _split_senders(senders: str) -> list[str]:
    """逗号分隔 wxid → 列表（去重、保序，空串 = 全部）。"""
    out: list[str] = []
    for item in str(senders or "").split(","):
        value = item.strip()
        if value and value not in out:
            out.append(value)
    return out


def _check_time(start: str, end: str) -> None:
    """时间格式/区间校验（400）。"""
    from app.summary import parse_time_range
    try:
        parse_time_range(start, end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/members", response_model=SummaryMembersOut)
def get_members(session: str = Query(min_length=1),
                start: str = Query(...),
                end: str = Query(...),
                senders: str = "",
                svc: SummaryService = Depends(get_summary_service)
                ) -> SummaryMembersOut:
    """全量成员 + 筛选后统计/预算（items 恒为全量）。"""
    _check_time(start, end)
    data = svc.members(session, start, end, _split_senders(senders))
    if data.get("error"):
        raise HTTPException(status_code=503, detail=str(data["error"]))
    return SummaryMembersOut(**data)


@router.post("/generate", response_model=SummaryOut)
def generate(body: SummaryGenerateIn,
             svc: SummaryService = Depends(get_summary_service)) -> SummaryOut:
    """生成总结（成功即落库；AI 失败不落库）。"""
    _check_time(body.start, body.end)
    if body.style not in STYLE_VALUES:
        raise HTTPException(status_code=400, detail="不支持的总结风格")
    try:
        row = svc.generate(body.session, body.start, body.end, body.senders,
                           body.style, body.custom_prompt)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SummaryOverBudgetError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except analyzer.AIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return SummaryOut(**row)


@router.get("/history", response_model=SummaryListOut)
def list_history(page: int = Query(default=1, ge=1),
                 page_size: int = Query(default=20, ge=1, le=200),
                 session: str = "",
                 date: str = "",
                 db: Database = Depends(get_db)) -> SummaryListOut:
    """历史分页（session 精确、date=YYYY-MM-DD）。"""
    if date:
        from datetime import datetime
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="日期格式应为 YYYY-MM-DD") from exc
    total, rows = db.list_group_summaries(
        page=page, page_size=page_size, session=session, date=date)
    return SummaryListOut(total=total, items=[SummaryOut(**r) for r in rows])


def _get_row_or_404(db: Database, summary_id: int) -> dict:
    """读取总结，不存在时 404（中文 detail）。"""
    row = db.get_group_summary(summary_id)
    if row is None:
        raise HTTPException(status_code=404, detail="总结不存在")
    return row


@router.get("/history/{summary_id}", response_model=SummaryOut)
def get_history(summary_id: int,
                db: Database = Depends(get_db)) -> SummaryOut:
    """历史详情。"""
    return SummaryOut(**_get_row_or_404(db, summary_id))


@router.delete("/history/{summary_id}")
def delete_history(summary_id: int,
                   db: Database = Depends(get_db)) -> dict:
    """删除一条历史总结（二次确认由前端负责）。"""
    _get_row_or_404(db, summary_id)
    db.delete_group_summary(summary_id)
    return {"ok": True}


@router.get("/history/{summary_id}/export")
def export_history(summary_id: int,
                   format: str = Query(default="txt"),
                   db: Database = Depends(get_db),
                   svc: SummaryService = Depends(get_summary_service)):
    """导出 txt / HTML（RFC5987 文件名，内容由服务端统一渲染）。"""
    fmt = str(format or "").strip().lower()
    if fmt not in ("txt", "html"):
        raise HTTPException(status_code=400, detail="导出格式仅支持 txt 或 html")
    row = _get_row_or_404(db, summary_id)
    start = str(row.get("time_start") or "").replace(":", "-").replace(" ", "-")
    end = str(row.get("time_end") or "").replace(":", "-").replace(" ", "-")
    name = str(row.get("session_name") or row.get("session_username") or "")
    base = f"群消息总结-{name}-{start}至{end}"
    # RFC5987：非 ASCII 文件名必须 percent-encode；ASCII fallback 防老客户端。
    disposition = (
        f'attachment; filename="summary.{fmt}"; '
        f"filename*=UTF-8''{quote(base, safe='')}.{fmt}"
    )
    if fmt == "txt":
        return PlainTextResponse(
            svc.render_txt(row),
            headers={"Content-Disposition": disposition},
            media_type="text/plain; charset=utf-8",
        )
    return HTMLResponse(
        svc.render_html(row),
        headers={"Content-Disposition": disposition},
    )


@router.get("/settings", response_model=SummarySettingsOut)
def get_summary_settings(db: Database = Depends(get_db)) -> SummarySettingsOut:
    """设置页配置区（预算 5 键 + 定时 2 键，均为 settings 点分键）。"""
    data = {key: db.get_setting(key, default)
            for key, default in _SETTINGS_DEFAULTS.items()}
    return SummarySettingsOut(**data)


@router.put("/settings", response_model=SummarySettingsOut)
def update_summary_settings(body: SummarySettingsUpdate,
                            db: Database = Depends(get_db)) -> SummarySettingsOut:
    """写入设置页配置（None=不变更；枚举、数值范围与定时 JSON 校验失败 422）。"""
    changes = body.model_dump(by_alias=True, exclude_none=True)
    for key, value in changes.items():
        text = str(value)
        if key == KEY_SUMMARY_BUDGET_MODE and text not in BUDGET_MODE_VALUES:
            raise HTTPException(status_code=422, detail="预算口径仅支持 money 或 tokens")
        if key == KEY_SUMMARY_OVER_LIMIT_ACTION and text not in OVER_LIMIT_ACTION_VALUES:
            raise HTTPException(status_code=422, detail="超限策略仅支持 block 或 truncate_recent")
        if key in (KEY_SUMMARY_BUDGET_MONEY, KEY_SUMMARY_BUDGET_TOKENS,
                   KEY_SUMMARY_TRUNCATE_RECENT_N):
            try:
                number = float(text)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="预算数值必须为数字") from exc
            if number < 0:
                raise HTTPException(status_code=422, detail="预算数值不能为负数")
        if key == KEY_SUMMARY_SCHEDULE_ENABLED and text not in ("0", "1"):
            raise HTTPException(status_code=422, detail="定时开关仅支持 0 或 1")
        if key == KEY_SUMMARY_SCHEDULE_CONFIGS:
            try:
                configs = json.loads(text)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="定时配置必须是 JSON 数组") from exc
            if not isinstance(configs, list):
                raise HTTPException(status_code=422, detail="定时配置必须是 JSON 数组")
            for item in configs:
                if not isinstance(item, dict) or not str(item.get("session") or ""):
                    raise HTTPException(status_code=422, detail="定时配置缺少群聊")
                if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d",
                                    str(item.get("time") or "18:00")):
                    raise HTTPException(status_code=422, detail="触发时间格式应为 HH:MM")
        db.set_setting(key, text)
    return get_summary_settings(db)
