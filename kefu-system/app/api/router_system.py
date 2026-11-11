# -*- coding: utf-8 -*-
"""运行状态与会话/消息查询路由。

路由（设计文档 3.2）：
- GET /api/status
- GET /api/sessions/search?keyword=&limit=20&kind=all
- GET /api/messages/{username}?before_seq=&limit=50
- GET /api/messages/{username}/images/{local_id}   图片解密（反馈 2）
- GET /api/system/retention | POST /api/system/purge          历史数据（反馈 7）
- GET /api/system/purge/last                                 上次清理留痕（本轮修复）
- GET /api/system/billing                                     AI 账单（反馈 13）
- GET/PUT /api/system/asr | POST /api/system/asr/download      语音转写（本地离线）
- WS  /ws/events
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query, Response, WebSocket, WebSocketDisconnect

from app import voice_asr
from app.api.deps import (
    get_db,
    get_notifier,
    get_pipeline,
    get_wechat_service,
)
from app.config import (
    KEY_ASR_ENABLED,
    KEY_RUNTIME_CACHE_DIR,
    KEY_RUNTIME_LAST_PURGE,
    check_cache_dir,
)
from app.db import Database, now_iso, read_monitor_max_targets
from app.schemas import (
    AnalysisHealth,
    AsrStatusOut,
    AsrUpdate,
    BillingDayOut,
    BillingOut,
    CacheDirOut,
    CacheDirUpdate,
    CacheShardStatus,
    MessageOut,
    MonitorQuota,
    PurgeRequest,
    PurgeResult,
    QueueStatus,
    RetentionOut,
    RetentionUpdate,
    SessionItemOut,
    StatusOut,
    WechatStatus,
    WorkerStatus,
)
from app.pipeline import Pipeline
from app.wechat_service import WeChatService

logger = logging.getLogger(__name__)

router = APIRouter()


def _billing_settings(db: Database) -> dict:
    """读取账单单价配置：input_price/output_price/unit（per_k|per_m）。"""
    unit = db.get_setting("billing.unit", "per_k")
    if unit not in ("per_k", "per_m"):
        unit = "per_k"
    return {
        "unit": unit,
        "input_price": db.get_float("billing.input_price", 0.0),
        "output_price": db.get_float("billing.output_price", 0.0),
    }


def _usage_cost(usage: dict, billing: dict) -> float:
    """按单价与单位换算一次调用/一日的费用（元）。

    per_k：元/千 tokens；per_m：元/百万 tokens。未配置单价时为 0。
    """
    divisor = 1000.0 if billing["unit"] == "per_k" else 1_000_000.0
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)
    return (pt / divisor * billing["input_price"]
            + ct / divisor * billing["output_price"])


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
    try:
        health = dict(pipeline.analysis_health)
    except Exception:  # 防御：健康度读取失败不阻塞状态接口
        logger.exception("读取分析健康度失败（按空值处理）")
        health = {}
    return StatusOut(
        listener_running=svc.listener_running,
        monitored_count=svc.monitored_count,
        wechat=WechatStatus(alive=svc.available,
                            visible=svc.probe_wechat_window()),
        queue=QueueStatus(size=pipeline.queue_size, max=pipeline.queue_max,
                          dropped_total=pipeline.dropped_total),
        worker=WorkerStatus(busy=pipeline.busy, max=2),
        monitor_quota=MonitorQuota(used=db.count_targets(),
                                   max=read_monitor_max_targets(db)),
        analysis_health=AnalysisHealth(
            last_error=str(health.get("last_error") or ""),
            consecutive_failures=int(health.get("consecutive_failures") or 0),
        ),
        ai_configured=ai_configured,
        ai_enabled=db.get_setting("ai.enabled", "1") != "0",
        watermark_saved_at=svc.watermark_saved_at,
        degraded_reason=svc.degraded_reason,
        unread_count=notifier.unread_count,
        today_cost=round(_usage_cost(
            db.usage_by_date(datetime.now().strftime("%Y-%m-%d")),
            _billing_settings(db)), 4),
        cache=_cache_status(svc),
    )


def _cache_status(svc: WeChatService) -> List[CacheShardStatus]:
    """读取解密缓存健康度（只读；异常降级为空列表，不影响状态接口）。"""
    try:
        items = svc.cache_status()
    except Exception:  # 防御：缓存健康度不可用不应让 /api/status 失败
        logger.exception("读取解密缓存健康度失败（按空列表处理）")
        return []
    result: List[CacheShardStatus] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            result.append(CacheShardStatus(**item))
        except Exception:  # 单分片字段异常不影响其他分片
            logger.exception("解密缓存健康度字段异常：%s", item)
    return result


# ---------------------------------------------------------------------------
# GET /api/sessions/search
# ---------------------------------------------------------------------------
@router.get("/api/sessions/search", response_model=List[SessionItemOut])
def search_sessions(
    keyword: str = "",
    limit: int = Query(default=20, ge=1, le=100),
    kind: str = Query(default="all", pattern="^(all|group|friend)$"),
    svc: WeChatService = Depends(get_wechat_service),
) -> List[SessionItemOut]:
    """会话搜索（群 + 好友，复用 search_contact/get_sessions；kind 过滤类型）。"""
    items = svc.search_sessions(keyword=keyword, limit=limit, kind=kind)
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
# GET /api/messages/{username}/images/{local_id}   图片解密（反馈 2）
# ---------------------------------------------------------------------------
@router.get("/api/messages/{username}/images/{local_id}",
            response_class=Response)
def get_message_image(username: str, local_id: int,
                      svc: WeChatService = Depends(get_wechat_service),
                      ) -> Response:
    """解密并返回会话中指定图片消息（image/jpeg|png|gif）。

    - 数据源：本地 .dat 缓存解密（群聊原图未点开时自动回退缩略图）；
    - 不做网络拉取（主理人决策）；无缓存/解密失败回 404，前端显示占位卡片。
    """
    data = svc.get_image_bytes(username, local_id)
    if not data:
        raise HTTPException(status_code=404, detail="图片本地缓存不存在或解密失败")
    media_type = "image/png" if data[:4] == b"\x89PNG" else \
        "image/gif" if data[:3] == b"GIF" else "image/jpeg"
    return Response(content=data, media_type=media_type)


# ---------------------------------------------------------------------------
# 历史数据管理（反馈 7）
# ---------------------------------------------------------------------------
@router.get("/api/system/retention", response_model=RetentionOut)
def get_retention(db: Database = Depends(get_db)) -> RetentionOut:
    """读取两表保留天数（仅手动清理时作为 keep_days 默认值，不再自动执行）。

    天数为「保留最近 N 个自然日（含今天）」，与清理按钮文案一致。
    """
    return RetentionOut(
        logs_days=db.get_int("retention.logs_days", 90),
        opportunities_days=db.get_int("retention.opportunities_days", 365),
    )


@router.put("/api/system/retention", response_model=RetentionOut)
def update_retention(body: RetentionUpdate,
                     db: Database = Depends(get_db)) -> RetentionOut:
    """更新保留天数（None = 不变更）。系统不做定时清理，仅手动触发。"""
    if body.logs_days is not None:
        db.set_setting("retention.logs_days", str(int(body.logs_days)))
    if body.opportunities_days is not None:
        db.set_setting("retention.opportunities_days",
                       str(int(body.opportunities_days)))
    return get_retention(db)


@router.post("/api/system/purge", response_model=PurgeResult)
def purge_history(body: PurgeRequest,
                  db: Database = Depends(get_db)) -> PurgeResult:
    """手动清理指定表中超过保留时长的历史数据（唯一的清理入口）。

    keep_days 语义为「保留最近 N 个自然日（含今天）」：N=1 只保留今天
    （截断点 = 今天 00:00:00），N=2 保留昨天与今天，以此类推。
    dry_run=True 时只统计将被删除的条数（预览），供前端弹出二次确认；
    dry_run=False 才真正删除，并把清理结果写入 settings（runtime.last_purge）
    留痕——不依赖日志文件，日志轮转失败时也能查到「数据被哪次清理删掉的」。
    """
    try:
        cutoff = db.purge_cutoff(body.keep_days)
        if body.dry_run:
            pending = db.count_older_than(body.table, body.keep_days)
            logger.info("清理预览：%s 将删除 %d 条（保留最近 %d 天含今天，截断点 %s）",
                        body.table, pending, body.keep_days, cutoff)
            return PurgeResult(table=body.table, deleted=pending,
                              dry_run=True, cutoff=cutoff)
        deleted = db.delete_older_than(body.table, body.keep_days)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    logger.info("手动清理：%s 删除 %d 条（保留最近 %d 天含今天，删除了 %s 之前的记录）",
                body.table, deleted, body.keep_days, cutoff)
    # 清理结果落库留痕（settings 表），与日志双保险
    record = {
        "table": body.table,
        "deleted": int(deleted),
        "keep_days": int(body.keep_days),
        "cutoff": cutoff,
        "at": now_iso(),
    }
    try:
        db.set_setting(KEY_RUNTIME_LAST_PURGE,
                       json.dumps(record, ensure_ascii=False))
    except Exception:  # 留痕失败不应让清理接口报错（删除已经完成）
        logger.exception("写入清理记录失败（已忽略）")
    return PurgeResult(table=body.table, deleted=deleted,
                       dry_run=False, cutoff=cutoff)


@router.get("/api/system/purge/last")
def get_last_purge(db: Database = Depends(get_db)) -> Dict[str, Any]:
    """上次手动清理记录（settings: runtime.last_purge）。

    无记录、或历史遗留值不是合法 JSON 对象时返回 ``{}``，前端按「暂无记录」展示。
    """
    raw = db.get_setting(KEY_RUNTIME_LAST_PURGE, "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("清理记录格式非法（已按空记录处理）：%s", raw)
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# AI 账单估算（反馈 13）
# ---------------------------------------------------------------------------
@router.get("/api/system/billing", response_model=BillingOut)
def get_billing(db: Database = Depends(get_db)) -> BillingOut:
    """AI 账单：今日与近 7 日 token 用量及按单价估算的费用（元）。"""
    billing = _billing_settings(db)
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_usage = db.usage_by_date(today_str)
    series = [
        BillingDayOut(date=d["date"], calls=d["calls"],
                      prompt_tokens=d["prompt_tokens"],
                      completion_tokens=d["completion_tokens"],
                      cost=round(_usage_cost(d, billing), 4))
        for d in db.usage_series(7)
    ]
    return BillingOut(
        unit=billing["unit"],
        input_price=billing["input_price"],
        output_price=billing["output_price"],
        today=BillingDayOut(date=today_str, calls=today_usage["calls"],
                            prompt_tokens=today_usage["prompt_tokens"],
                            completion_tokens=today_usage["completion_tokens"],
                            cost=round(_usage_cost(today_usage, billing), 4)),
        series=series,
    )


# ---------------------------------------------------------------------------
# 微信数据库解密缓存目录（可配置，默认客服系统运行目录下）
# ---------------------------------------------------------------------------
@router.get("/api/system/cache", response_model=CacheDirOut)
def get_cache_dir(wechat: WeChatService = Depends(get_wechat_service),
                  ) -> CacheDirOut:
    """读取缓存目录配置与实际占用（解密副本可达数百 MB）。"""
    return CacheDirOut(**wechat.cache_dir_info())


@router.put("/api/system/cache", response_model=CacheDirOut)
def update_cache_dir(body: CacheDirUpdate,
                     db: Database = Depends(get_db),
                     wechat: WeChatService = Depends(get_wechat_service),
                     ) -> CacheDirOut:
    """修改缓存目录；空值恢复默认。

    先做可写性校验（创建目录 + 写探针），避免保存后在后台重建线程里才
    失败。路径会在重启服务后生效（当前监听实例仍用旧目录）。
    """
    raw = (body.dir or "").strip()
    ok, reason, path = check_cache_dir(raw)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    db.set_setting(KEY_RUNTIME_CACHE_DIR, str(path) if raw else "")
    logger.info("缓存目录已更新为：%s（重启服务后生效）", path)
    return CacheDirOut(**wechat.cache_dir_info())


# ---------------------------------------------------------------------------
# 语音识别（本地离线 SenseVoice；音频绝不上传）
# ---------------------------------------------------------------------------
def _asr_payload(db: Database) -> dict:
    """组装 /api/system/asr 的响应字段（任何子项异常都降级为默认值）。"""
    transcriber = voice_asr.get_transcriber()
    try:
        available, reason = transcriber.available()
    except Exception:  # 依赖检测异常（缺 DLL/权限）不应让接口 500
        logger.exception("检测语音识别可用性失败（按不可用处理）")
        available, reason = False, "语音识别依赖检测失败，请查看服务日志"
    state = voice_asr.download_state()
    status = str(state.get("status") or voice_asr.STATUS_IDLE)
    model_ready = voice_asr.model_ready()
    model_size = voice_asr.model_size_bytes()
    # 模型已就位但从未触发过下载时，对外统一报 ready（避免前端看到 idle 却
    # 显示「未安装」的错位）；下载中保持真实进度状态。
    if status != voice_asr.STATUS_DOWNLOADING and model_ready:
        status = voice_asr.STATUS_READY
    downloaded = int(state.get("downloaded_bytes") or 0)
    total = int(state.get("total_bytes") or 0)
    if status == voice_asr.STATUS_READY and not downloaded:
        downloaded = total = model_size
    return {
        "enabled": db.get_setting(KEY_ASR_ENABLED, "1") != "0",
        "available": available,
        "reason": reason,
        "engine": voice_asr.ENGINE_NAME,
        "model_dir": str(voice_asr.ASR_MODEL_DIR),
        "model_ready": model_ready,
        "model_size_bytes": model_size,
        "decoder_ready": voice_asr.decoder_ready(),
        "download_status": status,
        "downloaded_bytes": downloaded,
        "total_bytes": total,
        "error": str(state.get("error") or ""),
    }


def _asr_status(db: Database) -> AsrStatusOut:
    """读取语音识别状态；任何异常都返回结构化降级状态（绝不 500）。"""
    try:
        return AsrStatusOut(**_asr_payload(db))
    except Exception:
        logger.exception("读取语音识别状态失败（返回降级状态）")
        return AsrStatusOut(engine=voice_asr.ENGINE_NAME)


@router.get("/api/system/asr", response_model=AsrStatusOut)
def get_asr_status(db: Database = Depends(get_db)) -> AsrStatusOut:
    """语音识别状态：开关 / 可用性 / 模型就位与下载进度。"""
    return _asr_status(db)


@router.put("/api/system/asr", response_model=AsrStatusOut)
def update_asr_status(body: AsrUpdate,
                      db: Database = Depends(get_db)) -> AsrStatusOut:
    """打开/关闭「语音消息自动转文字」（写 settings: asr.enabled）。"""
    db.set_setting(KEY_ASR_ENABLED, "1" if body.enabled else "0")
    logger.info("语音自动转文字已%s", "开启" if body.enabled else "关闭")
    return _asr_status(db)


@router.post("/api/system/asr/download", response_model=AsrStatusOut)
def download_asr_model(db: Database = Depends(get_db)) -> AsrStatusOut:
    """触发后台下载 SenseVoice 模型（幂等：下载中重复调用直接返回进度）。

    下载跑在后台线程（228MB，15 秒~数分钟），接口立即返回当前状态；
    前端轮询 GET /api/system/asr 展示进度。任何异常都不返回 500。
    """
    try:
        voice_asr.start_model_download()
    except Exception:
        logger.exception("触发语音模型下载失败（返回当前状态）")
    return _asr_status(db)


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
