# -*- coding: utf-8 -*-
"""图片内容识别（视觉模型）：判断是否车辆/汽车配件，并尽力提取 VIN。

用途（批次 6 需求 5）：微信群里大量消息只发一张实拍图（发动机、车壳、
轮毂等），纯文本分析看不到任何信息，这类「图片即卖货」的消息会被漏判。
本模块把图片交给用户自己配置的 OpenAI 兼容网关（与商机判定同一个 Key）
做一次视觉识别：

- 是汽车整车/汽车配件 → 管道按「卖货（sell）」方向产出商机，并带上部件名
  与（如能看清）17 位车架号；
- 不是 → 保持 skipped，不产生商机。

设计约束：
- **不新增依赖**：复用已安装的 openai SDK + Pillow；
- 图片先等比缩到长边 ≤ ``max_side``（默认 1024）并转 JPEG，控制 token 成本；
- 坏图（不完整/花屏）直接抛 :class:`VisionError`，**不消耗 token**；
- 所有失败都转成中文 :class:`VisionError`，绝不把原始堆栈抛给调用方；
- 不给视觉请求加 ``thinking`` 之类的 ``extra_body``（部分网关不认，失败
  会白跑一次请求）。
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 送模型前把长边缩到这个尺寸：既能看清配件/VIN，又不至于让 token 成本爆炸
VISION_MAX_SIDE_DEFAULT = 1024
VISION_JPEG_QUALITY = 85

# 视觉识别提示词：只让它输出一个 JSON 对象，字段固定
VISION_PROMPT = (
    "你是汽车行业的图片识别助手。请看这张来自微信聊天群的图片，判断它是不是"
    "「汽车整车」或「汽车配件/车辆部件」，并尽力读出车架号。\n"
    "判定为是（is_vehicle_part=true）：整车、发动机总成、变速箱、车壳、车门、"
    "大灯、保险杠、轮胎轮毂、座椅、中控台、线束、电脑板、拆车件等汽车相关实物照片。\n"
    "判定为不是（is_vehicle_part=false）：人物、自拍、聊天截图、风景、食物、"
    "纸质单据或单据截图、纯文字表格、与汽车无关的商品。\n"
    "part_name 用简体中文尽量具体（如「发动机总成」「前大灯」「铝合金轮毂」），"
    "不确定就给空字符串。\n"
    "vin 只有在图中能清楚读出 17 位车架号时才填写（只填 17 位字母数字，"
    "不含 I/O/Q），否则给空字符串。\n"
    "只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块标记，格式：\n"
    '{"is_vehicle_part": <true|false>, "part_name": "<中文部件名>", '
    '"vin": "<17 位车架号>", "reason": "<一句话说明>"}'
)

# VIN 合法字符：数字 + 字母，排除 I / O / Q（与车辆识别代号标准一致）
_VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")

# 「图片不完整或已损坏」= 可重试错误（微信还没把原图下载完）：
# 调用方**不要**把它写进 image_analysis 缓存，下次还能再试一次
MSG_INCOMPLETE = "图片不完整或已损坏"


class VisionError(Exception):
    """图片识别失败（消息为中文，可直接展示给用户）。"""


def _error_summary(exc: BaseException) -> str:
    """把 SDK/网络异常摘要成中文提示（401/403/429/超时 分类）。"""
    text = str(exc) or exc.__class__.__name__
    lowered = text.lower()
    if "401" in text or "403" in text:
        return "图片识别失败：Key 无效或无权限"
    if "429" in text:
        return "图片识别失败：额度不足或触发限流"
    if "timeout" in lowered or "timed out" in lowered or "超时" in text:
        return "图片识别失败：接口超时"
    return "图片识别失败：" + text[:120]


def _prepare_image(data: bytes, max_side: int) -> bytes:
    """等比缩放到长边 ≤ max_side，转 RGB 并存为 JPEG 字节。"""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        im.load()
        img = im.convert("RGB")
    width, height = img.size
    longest = max(width, height)
    if longest > max_side > 0:
        ratio = max_side / float(longest)
        img = img.resize((max(1, int(round(width * ratio))),
                          max(1, int(round(height * ratio)))))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=VISION_JPEG_QUALITY)
    return buf.getvalue()


def _normalize(raw: dict) -> dict:
    """规整模型输出（防御脏数据：类型/长度/VIN 合法性）。"""
    is_part = bool(raw.get("is_vehicle_part"))
    part = str(raw.get("part_name") or "").strip()[:40]
    vin = str(raw.get("vin") or "").strip().upper().replace(" ", "")
    # 模型偶尔多写/少写/带分隔符：不是严格 17 位就丢掉，避免脏 VIN 入库
    if not _VIN_RE.fullmatch(vin):
        vin = ""
    reason = str(raw.get("reason") or "").strip()[:80]
    if not is_part:
        part = ""
        vin = ""
    return {
        "is_vehicle_part": is_part,
        "part_name": part,
        "vin": vin,
        "reason": reason,
    }


def _parse_json(text: str) -> dict:
    """解析模型输出为 dict；兼容代码块包裹与前后多余文字。"""
    raw = str(text or "").strip()
    if not raw:
        raise VisionError("图片识别失败：模型返回内容为空")
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    candidates = [raw]
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    raise VisionError("图片识别失败：模型返回内容无法解析为 JSON")


def analyze_image(data: bytes, *, base_url: str = "", api_key: str = "",
                  model: str = "", timeout_sec: int = 60,
                  max_side: int = VISION_MAX_SIDE_DEFAULT,
                  client: Any = None) -> dict:
    """识别一张图片是否车辆/汽车配件，并提取 VIN。

    Args:
        data: 图片原始字节（jpg/png/gif）。
        base_url: OpenAI 兼容网关地址（与 ai.base_url 同一份配置）。
        api_key: 明文 API Key（调用方负责解密）。
        model: 视觉模型名（空 = 由调用方决定，通常沿用 ai.model）。
        timeout_sec: 请求超时（秒）。
        max_side: 送模型前的长边上限（控制 token 成本）。
        client: 测试注入的 openai 客户端替身；None 时按配置真实创建。

    Returns:
        ``{"is_vehicle_part": bool, "part_name": str, "vin": str,
        "reason": str, "prompt_tokens": int|None, "completion_tokens":
        int|None, "latency_ms": int}``

    Raises:
        VisionError: 图片为空/不完整/花屏、未配置、调用失败、解析失败。
            其中「图片不完整或已损坏」属于**可重试**错误（微信还没把原图
            下载完），调用方不应把它写进缓存。
    """
    raw = bytes(data or b"")
    if not raw:
        raise VisionError(MSG_INCOMPLETE)
    # 坏图先拦下：不消耗 token，也避免把坏图结论写进缓存
    from wechatauto.media import image_looks_broken, is_complete_image

    if not is_complete_image(raw) or image_looks_broken(raw):
        raise VisionError(MSG_INCOMPLETE)

    if client is None:
        if not base_url or not api_key or not model:
            raise VisionError("图片识别未配置：请先在设置页填写 AI 接口地址/Key/模型")
        try:
            from openai import OpenAI
        except ImportError as exc:      # 理论上 openai 已是必需依赖
            raise VisionError(f"openai SDK 未安装，无法识别图片：{exc}") from exc
        client = OpenAI(base_url=base_url, api_key=api_key,
                        timeout=float(timeout_sec))

    try:
        jpeg = _prepare_image(raw, max_side)
    except Exception as exc:
        raise VisionError(f"图片预处理失败：{exc}") from exc
    b64 = base64.b64encode(jpeg).decode("ascii")
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": VISION_PROMPT},
            {"type": "image_url",
             "image_url": {"url": "data:image/jpeg;base64," + b64}},
        ],
    }]
    started = time.monotonic()
    try:
        # 注意：视觉请求不下发 thinking/extra_body（部分网关不认会直接报错）
        resp = client.chat.completions.create(
            model=model, messages=messages,
            response_format={"type": "json_object"}, temperature=0.1)
    except Exception as exc:
        raise VisionError(_error_summary(exc)) from exc
    latency_ms = int((time.monotonic() - started) * 1000)

    text = ""
    try:
        text = resp.choices[0].message.content or ""
    except (AttributeError, IndexError):
        text = ""
    result = _normalize(_parse_json(text))
    usage = getattr(resp, "usage", None)
    result["prompt_tokens"] = (getattr(usage, "prompt_tokens", None)
                               if usage is not None else None)
    result["completion_tokens"] = (getattr(usage, "completion_tokens", None)
                                   if usage is not None else None)
    result["latency_ms"] = latency_ms
    logger.info("图片识别完成（%s，%d ms）：车辆/配件=%s 部件=%s VIN=%s",
                model, latency_ms, result["is_vehicle_part"],
                result["part_name"] or "-", result["vin"] or "-")
    return result


def image_settings(db: Any) -> dict:
    """读取图片识别配置：{enabled, model, base_url, api_key_enc, timeout_sec}。

    ``image.vision_model`` 为空时沿用 ``ai.model``（多数情况下视觉与文本
    用同一个多模态模型即可，避免用户重复填一遍）。
    """
    from app.config import (
        KEY_AI_API_KEY_ENC,
        KEY_AI_BASE_URL,
        KEY_AI_MODEL,
        KEY_AI_TIMEOUT_SEC,
        KEY_IMAGE_VISION_ENABLED,
        KEY_IMAGE_VISION_MODEL,
    )

    model = db.get_setting(KEY_IMAGE_VISION_MODEL, "").strip()
    return {
        "enabled": db.get_setting(KEY_IMAGE_VISION_ENABLED, "1") != "0",
        "model": model or db.get_setting(KEY_AI_MODEL, ""),
        "base_url": db.get_setting(KEY_AI_BASE_URL, ""),
        "api_key_enc": db.get_setting(KEY_AI_API_KEY_ENC, ""),
        "timeout_sec": db.get_int(KEY_AI_TIMEOUT_SEC, 60),
    }
