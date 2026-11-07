# -*- coding: utf-8 -*-
"""设置路由：AI 接口（Key 加密存储 + 测试连接）、分析判定、通知开关。

路由（设计文档 3.2）：
- GET/PUT /api/settings/ai
- POST    /api/settings/ai/test
- GET/PUT /api/settings/analysis
- PUT     /api/settings/notify
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app import analyzer
from app.api.deps import get_db
from app.config import (
    KEY_AI_API_KEY_ENC,
    KEY_AI_BASE_URL,
    KEY_AI_MODEL,
    KEY_AI_TEMPERATURE,
    KEY_AI_TIMEOUT_SEC,
    KEY_ANALYSIS_MERGE_WINDOW,
    KEY_ANALYSIS_OPP_MERGE_WINDOW,
    KEY_ANALYSIS_MIN_CONFIDENCE,
    KEY_ANALYSIS_PRESCREEN_ENABLED,
    KEY_ANALYSIS_PRESCREEN_KEYWORDS,
    KEY_ANALYSIS_PRESCREEN_REGEX,
    KEY_ANALYSIS_STRICTNESS,
    KEY_COMPLIANCE_CONFIRMED,
    KEY_NOTIFY_SOUND,
)
from app.crypto import encrypt_api_key, mask_key
from app.db import Database
from app.schemas import (
    AISettingsOut,
    AISettingsUpdate,
    AITestResult,
    AnalysisSettingsOut,
    AnalysisSettingsUpdate,
    NotifyUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


# ---------------------------------------------------------------------------
# AI 接口配置
# ---------------------------------------------------------------------------
@router.get("/ai", response_model=AISettingsOut)
def get_ai_settings(db: Database = Depends(get_db)) -> AISettingsOut:
    """AI 配置（Key 只回掩码，不落明文）。"""
    return AISettingsOut(
        base_url=db.get_setting(KEY_AI_BASE_URL, ""),
        api_key_masked=mask_key(db.get_setting(KEY_AI_API_KEY_ENC, "")),
        model=db.get_setting(KEY_AI_MODEL, ""),
        timeout_sec=db.get_int(KEY_AI_TIMEOUT_SEC, 60),
        temperature=db.get_float(KEY_AI_TEMPERATURE, 0.2),
    )


@router.put("/ai", response_model=AISettingsOut)
def update_ai_settings(body: AISettingsUpdate,
                       db: Database = Depends(get_db)) -> AISettingsOut:
    """保存 AI 配置（Key 留空则不变更）。

    首次保存需勾选合规知情确认（P0-合规）；确认后写
    ``compliance.confirmed=1``，分析管线才会启动。
    """
    db.set_setting(KEY_AI_BASE_URL, body.base_url.strip())
    db.set_setting(KEY_AI_MODEL, body.model.strip())
    db.set_setting(KEY_AI_TIMEOUT_SEC, str(body.timeout_sec))
    db.set_setting(KEY_AI_TEMPERATURE, str(body.temperature))
    if body.api_key.strip():
        # 仅非空/长度校验；测试连接失败不阻塞保存（用户可能离线配置）
        key = body.api_key.strip()
        if len(key) < 8:
            logger.warning("保存的 API Key 过短（仍按用户输入保存）")
        db.set_setting(KEY_AI_API_KEY_ENC, encrypt_api_key(key))
    if body.confirm_compliance:
        db.set_setting(KEY_COMPLIANCE_CONFIRMED, "1")
        logger.info("合规知情确认已完成")
    logger.info("AI 设置已保存（base_url=%s, model=%s）",
                body.base_url, body.model)
    return get_ai_settings(db)


@router.post("/ai/test", response_model=AITestResult)
def test_ai_connection(db: Database = Depends(get_db)) -> AITestResult:
    """测试连接：用已保存配置发 1 条最小补全。"""
    base_url = db.get_setting(KEY_AI_BASE_URL, "")
    model = db.get_setting(KEY_AI_MODEL, "")
    api_key_enc = db.get_setting(KEY_AI_API_KEY_ENC, "")
    if not base_url or not model or not api_key_enc:
        return AITestResult(ok=False, message="尚未配置完整的 AI 接口信息",
                            model=model)
    from app.crypto import decrypt_api_key
    client = analyzer.AIClient(
        base_url=base_url,
        api_key=decrypt_api_key(api_key_enc),
        model=model,
        timeout_sec=db.get_int(KEY_AI_TIMEOUT_SEC, 60),
        temperature=db.get_float(KEY_AI_TEMPERATURE, 0.2),
    )
    ok, message, latency_ms = client.test_connection()
    return AITestResult(ok=ok, message=message, latency_ms=latency_ms,
                        model=model)


# ---------------------------------------------------------------------------
# 分析判定配置
# ---------------------------------------------------------------------------
def _analysis_out(db: Database) -> AnalysisSettingsOut:
    return AnalysisSettingsOut(
        strictness=db.get_setting(KEY_ANALYSIS_STRICTNESS, "balanced"),
        min_confidence=db.get_int(KEY_ANALYSIS_MIN_CONFIDENCE, 60),
        prescreen_enabled=db.get_bool(KEY_ANALYSIS_PRESCREEN_ENABLED),
        prescreen_keywords=db.get_setting(KEY_ANALYSIS_PRESCREEN_KEYWORDS, ""),
        prescreen_regex=db.get_setting(KEY_ANALYSIS_PRESCREEN_REGEX, ""),
        merge_window_sec=db.get_int(KEY_ANALYSIS_MERGE_WINDOW, 60),
        opp_merge_window_sec=db.get_int(KEY_ANALYSIS_OPP_MERGE_WINDOW, 180),
    )


@router.get("/analysis", response_model=AnalysisSettingsOut)
def get_analysis_settings(db: Database = Depends(get_db)
                          ) -> AnalysisSettingsOut:
    """分析判定配置读取。"""
    return _analysis_out(db)


@router.put("/analysis", response_model=AnalysisSettingsOut)
def update_analysis_settings(body: AnalysisSettingsUpdate,
                             db: Database = Depends(get_db)
                             ) -> AnalysisSettingsOut:
    """保存判定档位/门槛/预筛/两级窗口（即时生效）。"""
    db.set_setting(KEY_ANALYSIS_STRICTNESS, body.strictness)
    db.set_setting(KEY_ANALYSIS_MIN_CONFIDENCE, str(body.min_confidence))
    db.set_setting(KEY_ANALYSIS_PRESCREEN_ENABLED,
                   "1" if body.prescreen_enabled else "0")
    db.set_setting(KEY_ANALYSIS_PRESCREEN_KEYWORDS,
                   body.prescreen_keywords.strip())
    db.set_setting(KEY_ANALYSIS_PRESCREEN_REGEX, body.prescreen_regex.strip())
    db.set_setting(KEY_ANALYSIS_MERGE_WINDOW, str(body.merge_window_sec))
    db.set_setting(KEY_ANALYSIS_OPP_MERGE_WINDOW,
                   str(body.opp_merge_window_sec))
    logger.info("分析设置已保存（strictness=%s, min_confidence=%d）",
                body.strictness, body.min_confidence)
    return _analysis_out(db)


# ---------------------------------------------------------------------------
# 通知配置
# ---------------------------------------------------------------------------
@router.put("/notify")
def update_notify_settings(body: NotifyUpdate,
                           db: Database = Depends(get_db)) -> dict:
    """提示音开关。"""
    db.set_setting(KEY_NOTIFY_SOUND, "1" if body.sound_enabled else "0")
    logger.info("通知设置已保存（sound_enabled=%s）", body.sound_enabled)
    return {"ok": True, "message": "通知设置已保存"}
