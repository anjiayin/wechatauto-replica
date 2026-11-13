# -*- coding: utf-8 -*-
"""设置路由：AI 接口（Key 加密存储 + 测试连接）、分析判定、通知开关。

路由（设计文档 3.2）：
- GET/PUT /api/settings/ai
- POST    /api/settings/ai/test
- GET/PUT /api/settings/analysis
- GET/PUT /api/settings/monitor
- GET/PUT /api/settings/image
- GET/PUT /api/settings/server     启动端口（重启生效）
- PUT     /api/settings/notify
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app import analyzer
from app.api.deps import get_db
from app.config import (
    CORRECTION_EXAMPLES_MAX,
    KEY_AI_API_KEY_ENC,
    KEY_AI_BASE_URL,
    KEY_AI_MODEL,
    KEY_AI_TEMPERATURE,
    KEY_AI_TIMEOUT_SEC,
    KEY_ANALYSIS_CORRECTION_EXAMPLES,
    KEY_ANALYSIS_FILTER_SELF,
    KEY_ANALYSIS_MERGE_WINDOW,
    KEY_ANALYSIS_OPP_MERGE_WINDOW,
    KEY_ANALYSIS_MIN_CONFIDENCE,
    KEY_ANALYSIS_PRESCREEN_ENABLED,
    KEY_ANALYSIS_PRESCREEN_KEYWORDS,
    KEY_ANALYSIS_PRESCREEN_REGEX,
    KEY_ANALYSIS_STRICTNESS,
    KEY_COMPLIANCE_CONFIRMED,
    KEY_IMAGE_VISION_ENABLED,
    KEY_IMAGE_VISION_MODEL,
    KEY_MONITOR_MAX_TARGETS,
    KEY_NOTIFY_SOUND,
    MERGE_WINDOW_SEC_DEFAULT,
)
from app.crypto import encrypt_api_key, mask_key
from app.db import Database, read_monitor_max_targets
from app.schemas import (
    AISettingsOut,
    AISettingsUpdate,
    AITestResult,
    AnalysisSettingsOut,
    AnalysisSettingsUpdate,
    ImageSettingsOut,
    ImageSettingsUpdate,
    MonitorSettingsOut,
    MonitorSettingsUpdate,
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
        disable_thinking=db.get_setting("ai.disable_thinking", "1") != "0",
        enabled=db.get_setting("ai.enabled", "1") != "0",
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
    db.set_setting("ai.disable_thinking", "1" if body.disable_thinking else "0")
    # 是否使用 AI 接口：关闭后管线改用「轻量筛选」规则判定商机（不再调 AI）
    db.set_setting("ai.enabled", "1" if body.enabled else "0")
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
        disable_thinking=db.get_setting("ai.disable_thinking", "1") != "0",
    )
    ok, message, latency_ms = client.test_connection()
    return AITestResult(ok=ok, message=message, latency_ms=latency_ms,
                        model=model)


# ---------------------------------------------------------------------------
# 分析判定配置
# ---------------------------------------------------------------------------
def _analysis_out(db: Database) -> AnalysisSettingsOut:
    from app.db import read_correction_examples
    return AnalysisSettingsOut(
        strictness=db.get_setting(KEY_ANALYSIS_STRICTNESS, "balanced"),
        min_confidence=db.get_int(KEY_ANALYSIS_MIN_CONFIDENCE, 60),
        prescreen_enabled=db.get_bool(KEY_ANALYSIS_PRESCREEN_ENABLED),
        prescreen_keywords=db.get_setting(KEY_ANALYSIS_PRESCREEN_KEYWORDS, ""),
        prescreen_regex=db.get_setting(KEY_ANALYSIS_PRESCREEN_REGEX, ""),
        merge_window_sec=db.get_int(KEY_ANALYSIS_MERGE_WINDOW,
                                    MERGE_WINDOW_SEC_DEFAULT),
        opp_merge_window_sec=db.get_int(KEY_ANALYSIS_OPP_MERGE_WINDOW, 180),
        correction_examples=read_correction_examples(db),
        filter_self_messages=db.get_setting(KEY_ANALYSIS_FILTER_SELF, "1") != "0",
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
    # 过滤自己发送的消息（批次 5 需求 ⑤，默认开启）
    db.set_setting(KEY_ANALYSIS_FILTER_SELF,
                   "1" if body.filter_self_messages else "0")
    # 漏判修正示例：None = 不变更（防部分更新误清空）；列表 = 全量替换
    #（服务端钳制：去空 + 截断 200 字/条 + 最多 20 条 FIFO 淘汰最旧）
    if body.correction_examples is not None:
        examples = [str(e).strip()[:200] for e in body.correction_examples
                    if str(e).strip()]
        examples = examples[-CORRECTION_EXAMPLES_MAX:]
        db.set_setting(KEY_ANALYSIS_CORRECTION_EXAMPLES,
                       json.dumps(examples, ensure_ascii=False))
    logger.info("分析设置已保存（strictness=%s, min_confidence=%d）",
                body.strictness, body.min_confidence)
    return _analysis_out(db)


# ---------------------------------------------------------------------------
# 监控设置（监控对象数量上限）
# ---------------------------------------------------------------------------
@router.get("/monitor", response_model=MonitorSettingsOut)
def get_monitor_settings(db: Database = Depends(get_db)
                         ) -> MonitorSettingsOut:
    """监控设置读取（当前仅 max_targets，越界值已钳制）。"""
    return MonitorSettingsOut(max_targets=read_monitor_max_targets(db))


@router.put("/monitor", response_model=MonitorSettingsOut)
def update_monitor_settings(body: MonitorSettingsUpdate,
                            db: Database = Depends(get_db)
                            ) -> MonitorSettingsOut:
    """保存监控对象数量上限（10~2000，即时生效）。"""
    db.set_setting(KEY_MONITOR_MAX_TARGETS, str(body.max_targets))
    logger.info("监控设置已保存（max_targets=%d）", body.max_targets)
    return get_monitor_settings(db)


# ---------------------------------------------------------------------------
# 图片内容识别（批次 6 需求 5：图片里的车辆/配件 → 按卖货产出商机）
# ---------------------------------------------------------------------------
def _image_out(db: Database) -> ImageSettingsOut:
    """图片识别设置读取（模型空 = 沿用 ai.model，此处按实际生效值回显）。"""
    return ImageSettingsOut(
        enabled=db.get_setting(KEY_IMAGE_VISION_ENABLED, "1") != "0",
        model=db.get_setting(KEY_IMAGE_VISION_MODEL, ""),
    )


@router.get("/image", response_model=ImageSettingsOut)
def get_image_settings(db: Database = Depends(get_db)) -> ImageSettingsOut:
    """图片识别设置读取。"""
    return _image_out(db)


@router.put("/image", response_model=ImageSettingsOut)
def update_image_settings(body: ImageSettingsUpdate,
                          db: Database = Depends(get_db)) -> ImageSettingsOut:
    """保存图片识别开关与视觉模型（留空 = 沿用 ai.model）。"""
    db.set_setting(KEY_IMAGE_VISION_ENABLED, "1" if body.enabled else "0")
    db.set_setting(KEY_IMAGE_VISION_MODEL, body.model.strip())
    logger.info("图片识别设置已保存（enabled=%s, model=%s）",
                body.enabled, body.model.strip() or "沿用 ai.model")
    return _image_out(db)


# ---------------------------------------------------------------------------
# 启动端口（设置页可直接改，重启服务后生效）
# ---------------------------------------------------------------------------
# 与 run.py 的配置键保持一致（那边负责启动时读取，这里负责读写设置）
SETTING_KEY_RUN_HOST = "run.host"
SETTING_KEY_RUN_PORT = "run.port"
DEFAULT_RUN_HOST = "127.0.0.1"
DEFAULT_RUN_PORT = 8765


class ServerSettingsOut(BaseModel):
    """监听端口设置（保存值 + 当前进程实际监听值）。"""

    host: str = DEFAULT_RUN_HOST          # 已保存/下次启动生效的地址
    port: int = DEFAULT_RUN_PORT          # 已保存/下次启动生效的端口
    running_host: str = ""                # 当前进程实际监听地址（探测不到为空）
    running_port: int = 0                 # 当前进程实际监听端口（0 = 未知）
    restart_required: bool = False        # 保存值与运行值不一致 → 需重启
    default_port: int = DEFAULT_RUN_PORT  # 「恢复默认」用
    address: str = ""                     # 下次启动后的访问地址


class ServerSettingsUpdate(BaseModel):
    """保存监听端口（1~65535；越界由 Pydantic 拦成 422）。"""

    port: int = Field(ge=1, le=65535)


def _running_address(request: Request) -> tuple[str, int]:
    """从 ASGI scope 取当前进程实际监听的 (host, port)。

    ``scope["server"]`` 是 uvicorn 绑定的地址（不是请求 Host），dev 模式下
    前端走 vite 代理也能拿到后端真实端口。取不到时返回 ("", 0)。
    """
    try:
        server = (request.scope or {}).get("server") or ("", 0)
        host = str(server[0] or "")
        port = int(server[1] or 0)
    except (TypeError, ValueError, IndexError):
        return "", 0
    return host, port


def _server_out(db: Database, request: Request) -> ServerSettingsOut:
    """组装端口设置（保存值 + 运行值 + 是否需重启）。"""
    host = db.get_setting(SETTING_KEY_RUN_HOST, DEFAULT_RUN_HOST) \
        or DEFAULT_RUN_HOST
    try:
        port = int(db.get_setting(SETTING_KEY_RUN_PORT, str(DEFAULT_RUN_PORT)))
    except (TypeError, ValueError):
        port = DEFAULT_RUN_PORT
    if not 1 <= port <= 65535:
        port = DEFAULT_RUN_PORT
    run_host, run_port = _running_address(request)
    # 地址按「保存值」给出：这里是「重启后从哪个地址访问」
    address = f"http://{host}:{port}"
    return ServerSettingsOut(
        host=host, port=port,
        running_host=run_host, running_port=run_port,
        restart_required=bool(run_port and run_port != port),
        default_port=DEFAULT_RUN_PORT,
        address=address,
    )


@router.get("/server", response_model=ServerSettingsOut)
def get_server_settings(request: Request,
                        db: Database = Depends(get_db)) -> ServerSettingsOut:
    """读取启动端口设置（含当前进程实际监听端口）。"""
    return _server_out(db, request)


@router.put("/server", response_model=ServerSettingsOut)
def update_server_settings(body: ServerSettingsUpdate, request: Request,
                           db: Database = Depends(get_db)) -> ServerSettingsOut:
    """保存启动端口（重启服务后生效）。

    只写 ``run.port``：启动入口 run.py 的优先级是
    「命令行 > 环境变量 > 已保存配置 > 默认 8765」，所以如果用户是用
    ``--port`` 或 ``KEFU_PORT`` 启动的，下次仍以那边为准（页面上会提示）。
    """
    db.set_setting(SETTING_KEY_RUN_PORT, str(int(body.port)))
    logger.info("启动端口已保存（run.port=%d，重启服务后生效）", body.port)
    return _server_out(db, request)


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


# ---------------------------------------------------------------------------
# AI 账单单价（反馈 13）
# ---------------------------------------------------------------------------
class BillingSettingsUpdate(BaseModel):
    """AI 账单单价配置：输入/输出单价 + 计价单位。"""
    input_price: float = Field(ge=0, le=10000)
    output_price: float = Field(ge=0, le=10000)
    unit: str = Field(pattern="^(per_k|per_m)$")   # per_k=元/千tokens per_m=元/百万tokens


@router.post("/billing")
def update_billing_settings(body: BillingSettingsUpdate,
                            db: Database = Depends(get_db)) -> dict:
    """保存 AI 账单单价（估算当日/近 7 日费用用）。"""
    db.set_setting("billing.input_price", repr(float(body.input_price)))
    db.set_setting("billing.output_price", repr(float(body.output_price)))
    db.set_setting("billing.unit", body.unit)
    logger.info("AI 账单单价已保存（in=%s out=%s unit=%s）",
                body.input_price, body.output_price, body.unit)
    return {"ok": True, "message": "AI 单价已保存"}
