# -*- coding: utf-8 -*-
"""AI 商机分析：openai SDK 客户端、Prompt 构造（三档严格度）、JSON 解析、预筛。

判定严格度与门槛注入方式（技术方案 5.4）：
- strictness 替换模板中的档位标签与对应细则段；
- min_confidence 不进 Prompt，在管线代码侧过滤（below_threshold）；
- 启用的商机类型每次分析时实时从 DB 读取注入。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, List, Optional

from app.config import CORRECTION_EXAMPLES_MAX

logger = logging.getLogger(__name__)


class AIError(Exception):
    """AI 调用/解析失败（管线捕获后记录 error 日志并重试）。"""


# ---------------------------------------------------------------------------
# Prompt 模板（技术方案 5.2，禁止改动语义）
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT_TEMPLATE = """你是微信群聊/私聊消息的销售商机分析助手。请判断下面的消息内容是否表达了明确的业务合作意向（采购、委托、询价、求供等），并按要求输出。

## 商机类型定义（仅当消息命中以下任一类型时才算命中；列表可能为空，为空时一律 hit=false）
{types_block}

## 判定严格度：{strictness_label}
{strictness_rules}

## 判定原则
1. 只基于消息文本判断，不要臆测发送人未表达的内容。
2. 广告、闲聊、表情、纯问候一律 hit=false。
3. points 提取发送人表达的具体需求要素（标的/数量/时间/价格/地点等），原文没有的不要编造。
4. quote 必须是原文的逐字摘录。
5. 只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块标记。
{examples_block}

## 输出 JSON 格式
{{"hit": <bool>, "type": <string|null>, "points": <string[]>, "confidence": <0-100>, "quote": <string>}}"""

# 修正示例段落头部（漏判修正：用户确认过的真实商机消息示例注入 Prompt）
_EXAMPLES_HEADER = (
    "## 商机示例（用户确认）\n"
    "以下是用户确认过的真实商机消息示例，与此相似的消息"
    "（尤其包含配件 OE 号、车型、配件名称、价格、数量等交易要素的）应判定为商机："
)

_STRICTNESS_LABELS = {
    "lenient": "宽松（lenient）",
    "balanced": "均衡（balanced）",
    "strict": "严格（strict）",
}

_STRICTNESS_RULES = {
    "lenient": "宽松(lenient)：消息包含与任一类型疑似相关的意向、询问即视为命中，"
               "confidence 给 40-70 区间。",
    "balanced": "均衡(balanced)：消息表达了较明确的合作/购买/委托意向"
                "（有标的物+意向动作）才命中，confidence 给 50-90 区间。",
    "strict": "严格(strict)：必须有具体、可执行的需求表达"
              "（标的+数量/时间/价格中至少两项），模糊询问不命中，"
              "confidence 给 70-100 区间。",
}


def format_examples_block(examples: Optional[List[str]]) -> str:
    """构造修正示例段落（最多 20 条，每条截断 100 字；空列表返回空串）。"""
    items = [str(e or "").strip()[:100] for e in (examples or [])]
    items = [e for e in items if e][:CORRECTION_EXAMPLES_MAX]
    if not items:
        return ""
    lines = [f"{i}. {e}" for i, e in enumerate(items, start=1)]
    return "\n" + _EXAMPLES_HEADER + "\n" + "\n".join(lines)


def build_system_prompt(types: List[dict], strictness: str = "balanced",
                        examples: Optional[List[str]] = None) -> str:
    """构造系统提示词。

    Args:
        types: 启用的商机类型 dict 列表（name/description）。
        strictness: lenient / balanced / strict。
        examples: 用户确认过的真实商机消息示例（漏判修正，最多注入 20 条）。
    """
    strictness = strictness if strictness in _STRICTNESS_LABELS else "balanced"
    if types:
        lines = [
            f"{i}. 【{t.get('name', '')}】{t.get('description', '')}"
            for i, t in enumerate(types, start=1)
        ]
        types_block = "\n".join(lines)
    else:
        types_block = "（当前未配置任何商机类型，一律 hit=false）"
    return _SYSTEM_PROMPT_TEMPLATE.format(
        types_block=types_block,
        strictness_label=_STRICTNESS_LABELS[strictness],
        strictness_rules=_STRICTNESS_RULES[strictness],
        examples_block=format_examples_block(examples),
    )


def build_user_message(unit_text: str, session_name: str, session_kind: str,
                       sender_name: str, sender_wxid: str, msg_count: int) -> str:
    """构造用户消息体（技术方案 5.3）。"""
    kind_label = "群聊" if session_kind == "group" else "私聊"
    return (
        f"会话：{session_name}（{kind_label}）\n"
        f"发送人：{sender_name}（wxid: {sender_wxid or '未知'}）\n"
        f"消息（按时间合并，共 {msg_count} 条）：\n"
        f"{unit_text}"
    )


# ---------------------------------------------------------------------------
# AI 输出解析
# ---------------------------------------------------------------------------
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_ai_json(text: str) -> dict:
    """解析 AI 输出为结构化结果；解析失败抛 AIError。

    解析失败时用正则提取首个 {...} 兜底（技术方案 5.4）。
    """
    raw = (text or "").strip()
    # 剥离 Markdown 代码块标记
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    candidates = [raw]
    m = _JSON_OBJECT_RE.search(raw)
    if m:
        candidates.append(m.group(0))
    for cand in candidates:
        try:
            data = json.loads(cand)
            if isinstance(data, dict):
                return _normalize_result(data)
        except (json.JSONDecodeError, ValueError):
            continue
    raise AIError("AI 返回内容无法解析为 JSON")


def _normalize_result(data: dict) -> dict:
    """校验/规整 AI 结果字段（防御性，避免脏数据入库）。"""
    hit = bool(data.get("hit"))
    type_name = data.get("type")
    if not isinstance(type_name, str) or not type_name.strip() or not hit:
        type_name = None
    points = data.get("points")
    if not isinstance(points, list):
        points = []
    points = [str(p).strip() for p in points if str(p).strip()][:20]
    try:
        confidence = int(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0
    confidence = max(0, min(100, confidence))
    quote = str(data.get("quote") or "").strip()
    if hit and (not type_name):
        # 命中但无类型名视为无效结果
        raise AIError("AI 返回 hit=true 但缺少命中类型名")
    return {
        "hit": hit,
        "type": type_name,
        "points": points,
        "confidence": confidence,
        "quote": quote,
    }


# ---------------------------------------------------------------------------
# 预筛
# ---------------------------------------------------------------------------
def prescreen(text: str, keywords: str, regex: str) -> bool:
    """轻量预筛：关键词（逗号分隔，中英文逗号均可）或正则任一命中即通过。

    关键词与正则均为空时视为全部通过（预筛不误伤）。
    """
    text = text or ""
    kws = [k.strip() for k in re.split(r"[,，]", keywords or "") if k.strip()]
    if kws and any(k in text for k in kws):
        return True
    if regex:
        try:
            if re.search(regex, text):
                return True
        except re.error as exc:
            logger.warning("预筛正则无效（按通过处理）：%s", exc)
            return True
    return not kws and not regex


# ---------------------------------------------------------------------------
# openai SDK 客户端
# ---------------------------------------------------------------------------
class AIClient:
    """OpenAI 兼容接口客户端（openai SDK v1）。

    Args:
        base_url: OpenAI 兼容网关地址（如 https://api.deepseek.com/v1）。
        api_key: 明文 API Key。
        model: 模型名。
        timeout_sec: 请求超时（秒）。
        temperature: 温度（建议 0.2 保证判定稳定）。
        client: 测试注入用（openai 客户端替身）；None 时按配置真实创建。
    """

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout_sec: int = 60, temperature: float = 0.2,
                 client: Any = None) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout_sec = timeout_sec
        self.temperature = temperature
        self._client = client

    def _ensure_client(self) -> Any:
        """懒加载 openai 客户端（导入失败给出中文错误）。"""
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise AIError(f"openai SDK 未安装，无法调用 AI 接口：{exc}") from exc
            if not self.base_url or not self.api_key:
                raise AIError("AI 接口未配置完整（API 地址或 Key 缺失）")
            self._client = OpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                timeout=float(self.timeout_sec),
            )
        return self._client

    def chat(self, system_prompt: str, user_message: str) -> str:
        """执行一次 chat.completions（json_object 模式），返回原始文本。"""
        client = self._ensure_client()
        started = time.monotonic()
        try:
            resp = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=self.temperature,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            raise AIError(f"AI 接口调用失败：{exc}") from exc
        logger.debug("AI 调用耗时 %.0f ms", (time.monotonic() - started) * 1000)
        content = ""
        try:
            content = resp.choices[0].message.content or ""
        except (AttributeError, IndexError):
            pass
        if not content:
            raise AIError("AI 返回内容为空")
        return content

    def test_connection(self) -> tuple[bool, str, int]:
        """测试连接：发 1 条最小补全。返回 (ok, 中文消息, 耗时 ms)。"""
        started = time.monotonic()
        try:
            client = self._ensure_client()
            client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "连接测试，请回复：OK"}],
                max_tokens=8,
                temperature=0,
            )
        except Exception as exc:
            return False, f"连接失败：{exc}", int((time.monotonic() - started) * 1000)
        latency = int((time.monotonic() - started) * 1000)
        return True, f"连接成功（模型：{self.model}）", latency


def analyze_unit(client: AIClient, unit_text: str, session_name: str,
                 session_kind: str, sender_name: str, sender_wxid: str,
                 msg_count: int, types: List[dict],
                 strictness: str,
                 examples: Optional[List[str]] = None) -> dict:
    """对一个分析单元执行完整 AI 判定，返回规整后的结果 dict。

    Args:
        examples: 用户确认过的真实商机消息示例（注入 Prompt，漏判修正）。

    Raises:
        AIError: 调用或解析失败。
    """
    system_prompt = build_system_prompt(types, strictness, examples=examples)
    user_message = build_user_message(
        unit_text, session_name, session_kind, sender_name, sender_wxid, msg_count
    )
    raw = client.chat(system_prompt, user_message)
    return parse_ai_json(raw)


def summarize_points(points: List[str]) -> str:
    """要点列表 → 换行拼接摘要（商机 summary 字段）。"""
    return "\n".join(p for p in points if p)
