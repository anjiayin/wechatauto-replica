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
    "大灯、保险杠、轮胎轮毂、座椅、中控台、线束、电脑板、拆车件等汽车相关实物照片；"
    "车辆铭牌/车架号铭牌/车门框铭牌也判 true（车商常拍铭牌询价配件，是重要线索）。\n"
    "判定为不是（is_vehicle_part=false）：人物、自拍、聊天截图、风景、食物、"
    "纸质单据或单据截图（发票、报价单、手写清单）、纯文字表格、与汽车无关的商品。\n"
    "注意：车辆铭牌是固定在车上的金属牌（上面有 VIN、发动机号、整车型号），"
    "属于汽车相关实物，不要因为「上面有文字」就当成纸质单据判 false。\n"
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

# 送模型的图像长边低于此值时，**不允许**输出 VIN（宁缺勿错）。
# 依据（2026-11-12 实测）：微信只给缩略图时是 136x180，其中 VIN 一行字符高度
# 仅 2~3 像素，人眼放大也读不出；而模型会照着「LFV3A2…」的厂商前缀愣编 17 位
# （实测编出 LFV3A23P2N3782165，真值是 LFV3A24F2A3102185）。同一张图用 603x799
# 的版本就能读对，可见瓶颈是分辨率不是提示词。600 的依据：136x180 拦截、
# 603x799 / 1706x1280 放行（字符高度 ≥8px 时才可能逐字辨认）。
VIN_MIN_IMAGE_SIDE = 600

# VIN 校验位（GB 16735 / ISO 3779）：第 9 位由前 17 位加权和模 11 得出，
# 余数 10 记作 X。这是拦「模型编 VIN」的确定性手段——实测真实 VIN 通过、
# 编造的那个差 7（应 9 实 2）。
_VIN_VALUES = {
    **{str(d): d for d in range(10)},
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}
_VIN_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)

# 低分辨率时追加到提示词末尾的强约束
LOW_RES_VIN_NOTE = (
    "\n【重要】当前图像分辨率较低（{w}x{h}），17 位车架号大概率无法逐字符辨认。"
    "vin 必须返回空字符串，**不要猜测、不要按车型或厂商前缀补全**。"
) 


def vin_check_digit_ok(vin: str) -> bool:
    """VIN 第 9 位校验位是否成立（长度/字符集不符返回 False）。

    注意：少数进口车或早期车型不填校验位，此时真实 VIN 也会判 False——
    对「宁可没有、也不要错」的场景这是可接受的取舍，被丢弃的 VIN 会写进
    ``result["vin_rejected"]`` 供排查。
    """
    text = str(vin or "").strip().upper()
    if len(text) != 17:
        return False
    total = 0
    for char, weight in zip(text, _VIN_WEIGHTS):
        value = _VIN_VALUES.get(char)
        if value is None:
            return False
        total += value * weight
    remainder = total % 11
    expected = "X" if remainder == 10 else str(remainder)
    return text[8] == expected

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


def _prepare_image(data: bytes, max_side: int) -> tuple:
    """等比缩放到长边 ≤ max_side，转 RGB 并存为 JPEG 字节。

    Returns:
        ``(jpeg 字节, (宽, 高))``：尺寸是**实际送给模型**的尺寸，调用方据此
        判断分辨率是否足以读 VIN（见 :data:`VIN_MIN_IMAGE_SIDE`）。
    """
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
    return buf.getvalue(), img.size


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
        "reason": str, "vin_rejected": str, "prompt_tokens": int|None,
        "completion_tokens": int|None, "latency_ms": int}``

        ``vin_rejected``：模型给了 VIN 但被丢弃的原因（空串 = 未丢弃）：
        ``"low_res"`` 图像分辨率不足、``"check_digit"`` 校验位不符。

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
        jpeg, sent_size = _prepare_image(raw, max_side)
    except Exception as exc:
        raise VisionError(f"图片预处理失败：{exc}") from exc
    # 分辨率不足时明确告诉模型「别猜」（代码侧还会兜一道，见下）
    low_res = max(sent_size) < VIN_MIN_IMAGE_SIDE
    prompt = VISION_PROMPT
    if low_res:
        prompt += LOW_RES_VIN_NOTE.format(w=sent_size[0], h=sent_size[1])
    b64 = base64.b64encode(jpeg).decode("ascii")
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
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
    # 两道「宁缺勿错」的 VIN 防线（顺序有意义：先看信息是否存在，再看好不好）
    result["vin_rejected"] = ""
    if result["vin"]:
        if low_res:
            result["vin_rejected"] = "low_res"
            logger.info(
                "丢弃模型给出的 VIN %s：送模型图像仅 %dx%d，低于 %dpx 无法逐字辨认",
                result["vin"], sent_size[0], sent_size[1], VIN_MIN_IMAGE_SIDE)
            result["vin"] = ""
        elif not vin_check_digit_ok(result["vin"]):
            result["vin_rejected"] = "check_digit"
            logger.info("丢弃模型给出的 VIN %s：校验位不符（疑似误读/编造）",
                        result["vin"])
            result["vin"] = ""
    usage = getattr(resp, "usage", None)
    result["prompt_tokens"] = (getattr(usage, "prompt_tokens", None)
                               if usage is not None else None)
    result["completion_tokens"] = (getattr(usage, "completion_tokens", None)
                                   if usage is not None else None)
    result["latency_ms"] = latency_ms
    logger.info("图片识别完成（%s，%d ms，%dx%d）：车辆/配件=%s 部件=%s VIN=%s%s",
                model, latency_ms, sent_size[0], sent_size[1],
                result["is_vehicle_part"], result["part_name"] or "-",
                result["vin"] or "-",
                f"（已丢弃误读 VIN：{result['vin_rejected']}）"
                if result["vin_rejected"] else "")
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
