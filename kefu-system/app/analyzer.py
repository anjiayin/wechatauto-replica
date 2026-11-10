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
import threading
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
5. direction 判断商机方向：发送人表达"求购/找货/收货/谁有"等买入意向为 "buy"；表达"出售/供货/有货/低价处理"等卖出意向为 "sell"；无法判断或非商机时为 ""。
6. 只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块标记。
{examples_block}

## 输出 JSON 格式
{{"hit": <bool>, "type": <string|null>, "points": <string[]>, "confidence": <0-100>, "quote": <string>, "direction": <"buy"|"sell"|"">}}"""

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
    # 买卖货方向（反馈 4）：buy/sell 之外的值一律归 ""（不明，历史不回溯）
    direction = str(data.get("direction") or "").strip().lower()
    if direction not in ("buy", "sell"):
        direction = ""
    if hit and (not type_name):
        # 命中但无类型名视为无效结果
        raise AIError("AI 返回 hit=true 但缺少命中类型名")
    return {
        "hit": hit,
        "type": type_name,
        "points": points,
        "confidence": confidence,
        "quote": quote,
        "direction": direction,
    }


# ---------------------------------------------------------------------------
# 预筛
# ---------------------------------------------------------------------------
def prescreen_hits(text: str, keywords: str, regex: str) -> List[str]:
    """返回命中的筛选条件描述（关键词原文 / 正则匹配到的文本）。

    与 :func:`prescreen` 同一判定口径，供「不使用 AI 接口」模式把命中的
    关键词写进商机要点，让用户知道为什么被判为商机。
    """
    text = text or ""
    hits: List[str] = []
    kws = [k.strip() for k in re.split(r"[,，]", keywords or "") if k.strip()]
    for k in kws:
        if k in text:
            hits.append(k)
    if regex:
        try:
            for m in re.finditer(regex, text):
                matched = m.group(0).strip()
                if matched and matched not in hits:
                    hits.append(matched)
                if len(hits) >= 10:
                    break
        except re.error as exc:
            logger.warning("预筛正则无效（忽略）：%s", exc)
    return hits[:10]


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
def _is_unsupported_param_error(exc: BaseException) -> bool:
    """判断异常是否为「网关不认识 extra_body 参数」（用于关闭思考模式降级）。

    只在错误信息确实指向思考/附加参数时才判定，避免把 Key 无效、
    模型不存在等 400 误当作参数不支持而重复请求。
    """
    text = str(exc).lower()
    tokens = ("thinking", "extra_body", "enable_thinking",
              "unexpected keyword", "unknown field", "unrecognized",
              "unsupported parameter", "extra fields")
    return any(t in text for t in tokens)


# ---------------------------------------------------------------------------
# 关闭思考模式的参数形态（多模型通用）
# ---------------------------------------------------------------------------
# 不同厂商关闭「思考/推理」的字段名不同，这里统一在 extra_body 中下发
# （extra_body 会原样并入请求 JSON body，等价于顶层字段）：
#   thinking.type      —— 智谱 GLM（bigmodel）、Anthropic / Claude 兼容网关
#   enable_thinking    —— 通义 Qwen、DashScope、vLLM 原生、硅基流动
#   reasoning_effort   —— OpenAI o 系 / gpt-5、Azure OpenAI（"none" 表示不推理）
# 顺序即「不知道模型族时的尝试顺序」：先沿用已验证有效的 thinking，再逐个试探。
THINKING_VARIANTS: dict = {
    "thinking": {"thinking": {"type": "disabled"}},
    "enable_thinking": {"enable_thinking": False},
    "reasoning_effort": {"reasoning_effort": "none"},
}
THINKING_ORDER = ("thinking", "enable_thinking", "reasoning_effort")

# (匹配片段, 首选形态)：片段同时匹配 base_url 与 model 的小写拼接串
_PROVIDER_HINTS = (
    ("bigmodel", "thinking"), ("zhipu", "thinking"), ("glm", "thinking"),
    ("anthropic", "thinking"), ("claude", "thinking"), ("moonshot", "thinking"),
    ("kimi", "thinking"), ("minimax", "thinking"),
    ("dashscope", "enable_thinking"), ("aliyun", "enable_thinking"),
    ("siliconflow", "enable_thinking"), ("qwen", "enable_thinking"),
    ("qwq", "enable_thinking"), ("vllm", "enable_thinking"),
    ("openai.com", "reasoning_effort"), ("azure", "reasoning_effort"),
    ("gpt-5", "reasoning_effort"), ("o1", "reasoning_effort"),
    ("o3", "reasoning_effort"), ("o4", "reasoning_effort"),
)

# 进程级「模型/网关 → 有效形态」记忆（"" 表示三种形态都被拒，只能走默认模式）
_THINKING_CACHE: dict = {}
_THINKING_LOCK = threading.Lock()


def _thinking_key(base_url: str, model: str) -> tuple:
    """缓存键：网关地址 + 模型名（各自归一化，忽略末尾斜杠与大小写）。"""
    return ((base_url or "").strip().rstrip("/").lower(),
            (model or "").strip().lower())


def thinking_variants(base_url: str, model: str) -> List[str]:
    """按模型族推断关闭思考参数的尝试顺序（首选形态在最前）。

    识别不出来时按 THINKING_ORDER 全量试探——试探只在「进程内首次遇到
    该模型」时发生，结果会被 ``_THINKING_CACHE`` 记住。
    """
    haystack = f"{base_url or ''} {model or ''}".lower()
    for hint, key in _PROVIDER_HINTS:
        if hint in haystack:
            return [key] + [k for k in THINKING_ORDER if k != key]
    return list(THINKING_ORDER)


def thinking_variant_cached(base_url: str, model: str) -> Optional[str]:
    """读取进程内已确认的形态；未确认过返回 None（"" 表示不支持）。"""
    with _THINKING_LOCK:
        return _THINKING_CACHE.get(_thinking_key(base_url, model))


def remember_thinking_variant(base_url: str, model: str, key: str) -> bool:
    """记住形态结论，返回是否为首次确认（用于日志去重）。"""
    cache_key = _thinking_key(base_url, model)
    with _THINKING_LOCK:
        if cache_key in _THINKING_CACHE:
            return False
        _THINKING_CACHE[cache_key] = key
        return True


def reset_thinking_cache() -> None:
    """清空进程级记忆（测试 / 用户改动 AI 配置后手动重置用）。"""
    with _THINKING_LOCK:
        _THINKING_CACHE.clear()


class AIClient:
    """OpenAI 兼容接口客户端（openai SDK v1）。

    Args:
        base_url: OpenAI 兼容网关地址（如 https://api.deepseek.com/v1）。
        api_key: 明文 API Key。
        model: 模型名。
        timeout_sec: 请求超时（秒）。
        temperature: 温度（建议 0.2 保证判定稳定）。
        client: 测试注入用（openai 客户端替身）；None 时按配置真实创建。
        disable_thinking: 关闭思考模式（默认开启）。

    ``disable_thinking`` 说明（实测结论）：GLM-5.x 等思考型模型默认会把
    "先推理再回答" 的推理 token 一起计入 completion，本系统只需要一个
    固定格式的 JSON（约 40 token），但默认模式下单条消息会生成 700~1600
    个 token（含隐藏 reasoning_content），耗时从 3 秒级放大到 30~60 秒且
    波动极大。通过 ``extra_body={"thinking": {"type": "disabled"}}`` 关闭后
    实测 27.2s → 2.9s、completion 1561 → 44。
    该参数**不绑定具体模型**：换模型后仍会下发，并按模型族自动切换形态
    （thinking / enable_thinking / reasoning_effort，见 ``THINKING_VARIANTS``）。
    网关拒绝某形态时自动试下一个，全部被拒则降级为默认模式并打日志，
    绝不因此报错；结论按 (base_url, model) 记入进程级缓存，后续请求
    不再重复试探。
    """

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout_sec: int = 60, temperature: float = 0.2,
                 client: Any = None, disable_thinking: bool = True) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout_sec = timeout_sec
        self.temperature = temperature
        self.disable_thinking = bool(disable_thinking)
        # 网关是否支持关闭思考参数（全部形态被拒时置 False）
        self._thinking_param_ok = True
        # 已确认生效的形态（None 表示尚未确认或确认不支持）
        self._thinking_variant: Optional[str] = None
        self._thinking_resolved = False
        self._thinking_candidates = thinking_variants(base_url, model)
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

    def _thinking_plan(self) -> List[str]:
        """本轮请求依次尝试的形态列表（"" 表示不带参数，即默认模式）。"""
        if not self.disable_thinking:
            return [""]
        if self._thinking_resolved:
            return [self._thinking_variant or ""]
        cached = thinking_variant_cached(self.base_url, self.model)
        if cached is not None:
            self._thinking_resolved = True
            self._thinking_variant = cached or None
            self._thinking_param_ok = bool(cached)
            return [cached]
        return list(self._thinking_candidates) + [""]

    def _note_thinking_ok(self, key: str) -> None:
        """记录本次请求的思考参数结论（进程级复用，按 base_url+model）。"""
        self._thinking_resolved = True
        self._thinking_variant = key or None
        self._thinking_param_ok = bool(key)
        if not self.disable_thinking:
            return
        if not remember_thinking_variant(self.base_url, self.model, key):
            return
        if key:
            logger.info(
                "已确认模型 %s 关闭思考的参数形态：%s（进程内后续复用，"
                "不再试探）", self.model, key)
        else:
            logger.warning(
                "模型 %s 不接受任何关闭思考参数（%s 均被网关拒绝），已降级"
                "为默认模式：响应更慢且波动更大，建议更换支持该参数的模型",
                self.model, "/".join(THINKING_ORDER))

    def thinking_label(self) -> str:
        """当前实例的思考模式状态（日志/连接测试展示用）。"""
        if not self.disable_thinking:
            return "开关已关闭"
        if self._thinking_variant:
            return self._thinking_variant
        if self._thinking_resolved:
            return "不支持（已降级）"
        return "待确认"

    def _create(self, client: Any, messages: list, **extra: Any) -> Any:
        """发起 chat.completions；思考参数按形态逐个试探，失败自动降级。"""
        kwargs = dict(extra)
        kwargs.setdefault("temperature", self.temperature)
        last_exc: Optional[BaseException] = None
        for key in self._thinking_plan():
            attempt = dict(kwargs)
            if key:
                attempt["extra_body"] = dict(THINKING_VARIANTS[key])
            try:
                resp = client.chat.completions.create(
                    model=self.model, messages=messages, **attempt)
            except Exception as exc:
                # "" = 已无参数可退，或错误与参数无关（如 Key 无效）：直接抛
                if not key or not _is_unsupported_param_error(exc):
                    raise
                last_exc = exc
                logger.warning(
                    "模型 %s 不接受思考参数 %s（%s），改试下一形态",
                    self.model, key, str(exc)[:100])
                continue
            self._note_thinking_ok(key)
            return resp
        if last_exc is not None:
            raise last_exc
        raise AIError("AI 请求未执行（无可用请求形态）")

    def chat(self, system_prompt: str, user_message: str) -> str:
        """执行一次 chat.completions（json_object 模式），返回原始文本。

        副作用：把本次调用的 token 用量写入 ``self.last_usage``
        （反馈 13 账单估算数据源；usage 缺失或调用失败时为 None）。
        """
        self.last_usage: Optional[dict] = None
        client = self._ensure_client()
        started = time.monotonic()
        try:
            resp = self._create(client, [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ], response_format={"type": "json_object"})
        except Exception as exc:
            raise AIError(f"AI 接口调用失败：{exc}") from exc
        logger.debug("AI 调用耗时 %.0f ms", (time.monotonic() - started) * 1000)
        # 记录 token 用量（部分兼容网关不返回 usage，防御性读取）
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self.last_usage = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
            }
        content = ""
        try:
            content = resp.choices[0].message.content or ""
        except (AttributeError, IndexError):
            pass
        if not content:
            raise AIError("AI 返回内容为空")
        return content

    def test_connection(self) -> tuple[bool, str, int]:
        """测试连接：发 1 条最小补全。返回 (ok, 中文消息, 耗时 ms)。

        与正式分析一样关闭思考模式，否则「连接测试」会因隐藏推理 token
        显示成 10 秒以上，与真实分析延迟不可比。消息里附带实际生效的
        参数形态，便于换模型后确认该模型是否支持关闭思考。
        """
        started = time.monotonic()
        try:
            client = self._ensure_client()
            self._create(client, [{"role": "user", "content": "连接测试，请回复：OK"}],
                         max_tokens=8, temperature=0)
        except Exception as exc:
            return False, f"连接失败：{exc}", int((time.monotonic() - started) * 1000)
        latency = int((time.monotonic() - started) * 1000)
        return True, (f"连接成功（模型：{self.model} · 思考参数："
                      f"{self.thinking_label()}）"), latency


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
    result = parse_ai_json(raw)
    # 附加本次调用 token 用量（反馈 13）；无 usage（兼容网关）时缺省 None
    usage = getattr(client, "last_usage", None) or {}
    result["prompt_tokens"] = usage.get("prompt_tokens")
    result["completion_tokens"] = usage.get("completion_tokens")
    return result


def summarize_points(points: List[str]) -> str:
    """要点列表 → 换行拼接摘要（商机 summary 字段）。"""
    return "\n".join(p for p in points if p)
