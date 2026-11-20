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
{direction_block}{image_block}{group_at_block}{examples_block}

## 输出 JSON 格式
{{"hit": <bool>, "type": <string|null>, "points": <string[]>, "confidence": <0-100>, "quote": <string>, "direction": <"buy"|"sell"|"">}}"""

# 修正示例段落头部（漏判修正：用户确认过的真实商机消息示例注入 Prompt）
_EXAMPLES_HEADER = (
    "## 商机示例（用户确认）\n"
    "以下是用户确认过的真实商机消息示例，与此相似的消息"
    "（尤其包含配件 OE 号、车型、配件名称、价格、数量等交易要素的）应判定为商机："
)

# 群聊 @人判定指引（批次 5 需求 ④）：群聊消息 @某人 通常是回复对方先前的
# 报价/供货/询价，是商机判定的重要上下文线索（仅群聊注入 Prompt）。
_GROUP_AT_GUIDANCE = """
7. 群聊指引：消息中 @某人 通常意味着发送人在**回复被 @ 者此前的报价、供货、询价或求助内容**（如 "@某某 45一个出" 是对对方询价的报价回复），应结合上下文判断是否商机；被 @ 的人的名字（已还原为群昵称/备注）是重要的上下文线索，quote 保留原文中的 @ 部分。
"""

# 买卖方向判定独立小节（批次 6 需求 3）。
# 背景：原判定原则第 5 条只有一句「求购= buy / 出售= sell」，模型容易把
# 「留联系方式求货」判成卖货（实测「那个有车壳有的打电话15887775311」
# 被判成 sell，实际是买货）。这里把方向判定升级为独立小节，给出正反例
# 和明确的「联系方式≠卖货」约束。
# 注意：本常量会被 _SYSTEM_PROMPT_TEMPLATE.format() 注入，正文里不能出现
# 未转义的花括号，否则 format 会抛 KeyError。
_DIRECTION_GUIDANCE = """
## 买卖方向判定（direction）
direction 表示**发送人自己想买还是想卖**，与消息里出现什么要素无关，只看发送人的意图动作：
- 判定为 "buy"（发送人要求购/找货）：出现联系方式（手机号/电话/微信/私聊）+ 求货措辞（有的 / 有货的 / 谁有 / 联系我 / 打我电话 / 报价给我 / 私聊我）；或出现「有 / 谁有 / 求 / 求购 / 收 / 找 / 要 / 来一套 / 到货了吗」这类求入口吻。
- 判定为 "sell"（发送人要出售/供货）：出现「出 / 卖 / 现货 / 大量 / 甩 / 处理 / 低价 / 可以发货 / 库存」这类供出口吻。
- 只有图片、实物展示、库存照片且**没有任何求购措辞**时，判定为 "sell"。
- 无法判断或非商机时输出空字符串 ""。
- 关键约束：**联系方式只是联系渠道，不能因为出现手机号/电话/微信就判成卖货**。留电话往往正是「谁有货就打我电话」的求购信号，必须结合求货/供货措辞判断方向。

正例（正确输出）：
1. 「那个有车壳有的打电话15887775311」→ buy（发送人在求车壳，让有货的人打他电话；手机号是联系方式不是卖货证据）
2. 「谁有 LSGMJ5P578V016154 这个发动机，报价给我」→ buy（谁有 + 报价给我 = 求购）
3. 「42431-0K090 现货 200 个，低价处理，可以发货」→ sell（现货 + 低价处理 + 可以发货 = 供货）

反例（易错，必须避免）：
1. 「那个有车壳有的打电话15887775311」**不能**判成 sell —— 出现手机号就当成卖货是错的，这里是求购；正确是 buy。
2. 「求购 31340-32080 减震，有的私聊我」**不能**判成 sell —— 求购 + 私聊我 是求对方联系自己，正确是 buy。
"""

# 图片消息识别结论段落（图片识别链路）。
# 背景：图片识别成功后，管道会合成一条
# 「[图片] 识别：车辆/汽车配件（前保险杠）」的文本送进分析。文本模型看不到
# 图，只看到这句既无数量价格、又无意向措辞的短句，按「只基于文本判断、
# 不要臆测」只能判 hit=false——视觉识别白花钱（实测「车辆铭牌」稳定未命中，
# 「前保险杠」在 60~70 分间抖动，卡在 min_confidence 门槛）。
# 这里把标记的含义讲清楚：它等价于「已经看过实物照片」。
# 注意：本常量会被 _SYSTEM_PROMPT_TEMPLATE.format() 注入，正文里不能出现
# 未转义的花括号，否则 format 会抛 KeyError。
_IMAGE_HINT_GUIDANCE = """
## 图片消息识别结论（重要）
消息里出现形如「[图片] 识别：车辆/汽车配件（前保险杠）」的字样时，说明系统已经用视觉模型看过这张实拍图，并确认图中是**汽车整车或汽车配件实物**，括号里是识别出的具体部件（如保险杠、发动机、变速箱、轮毂、车辆铭牌）。
遇到这种标记，按以下规则判定：
- 这是「看到实物照片」的等价证据，**不受**判定原则第 1 条「不要臆测」限制——实物照片本身就是交易线索，车商互发配件图通常是在报现货、问件或询价。
- 即使没有任何求购/出售措辞、没有数量价格、句子很短，也**必须 hit=true**，confidence 不低于 70。
- 商机类型优先填与汽车/配件相关的类型（如「汽车配件」）；type 与 points 里的部件名用括号中的具体部件，points 至少含一条「图片识别：<部件名>」。
- direction 默认 "sell"（发送人在展示现货）；但若同一单元里另有明确求购措辞（谁有 / 求购 / 找货 / 有的私聊我），按发送人的真实意图判 "buy"。
- quote 原样摘录含该标记的那一行，不要改写、不要省略括号里的部件名。
"""

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
                        examples: Optional[List[str]] = None,
                        group_chat: bool = False) -> str:
    """构造系统提示词。

    Args:
        types: 启用的商机类型 dict 列表（name/description）。
        strictness: lenient / balanced / strict。
        examples: 用户确认过的真实商机消息示例（漏判修正，最多注入 20 条）。
        group_chat: 是否群聊会话（群聊注入「@某人 = 回复对方」的判定指引，
            批次 5 需求 ④）。
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
        direction_block=_DIRECTION_GUIDANCE,
        image_block=_IMAGE_HINT_GUIDANCE,
        group_at_block=_GROUP_AT_GUIDANCE if group_chat else "",
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
# OE 宽松识别（批次 7 需求 1）：用户输入的 OE 号经常不标准——``8K0-615-301``
# 也可能写成 ``8K0 615 301``、``8K0.615.301``、``1K0 807 217 C``，甚至被
# 分隔符拆开的 VIN。现有 5 类 OE 正则够用，缺的是「分隔符把一段编号切断」。
#
# **绝对不能对全文做「去分隔符后再跑正则」**（实测过的坑）：合并窗口的文本是
# ``[HH:MM] 发送人: 内容`` 多行拼接，发送人用户名本身就在被分析文本里，而
# 用户名/网址/日期里到处是「字母数字 + 分隔符」，全文紧凑化会把它们粘成
# 8~15 位字母数字串去撞 OE 规则第 5 类：
#   ``zhaoyu_4932403`` → ``zhaoyu4932403``（群成员用户名）
#   ``https://www.cpfly.top/`` → ``wwwcpflytop``（网址）
#   ``2026-11-11 20:56:21`` → ``2026111120``（日期 + 时间）
# 真实语料上全文紧凑化净增 54 条、几乎全是这类误召回。
#
# 因此改成两步：先取**局部候选片段**（片段内部允许带分隔符），再对候选做
# **形状约束**（段长/段数/必须含数字与大小写等），只接受像 OE 的片段。
#
# 候选片段内的分隔符集合：空格（含全角）、``-``、``.``、``/``、``·``、``•``，
# 加上**全角减号与各种连字符变体**（中文输入法 / Word / 企业微信转贴极易打出
# 全角 ``－`` U+FF0D，以及 U+2010/2011/2012/2013/FF63 这些长短横线）。
# **不含下划线**（下划线是用户名/文件名特征，``NoO_x1ao9``、``zhaoyu_4932403``
# 就是靠它被排除的），也**不含冒号**（冒号会把时间 ``20:56`` 粘坏）。
# **刻意不含** em dash U+2014（中文破折号，用于断句而非分隔件号）与波浪线
# U+FF5E/``~``：后者会把 ``8000~12000元`` 这类价格区间粘成假 OE
# （实测 ``800012000`` 会被 A 类「9~13 位纯数字」命中）。
_OE_SEP_CHARS = (" \u3000-./\u00b7\u2022"
                 + "\uff0d\u2010\u2011\u2012\u2013\ufe63")
_OE_SEP_RE = re.compile("[" + re.escape(_OE_SEP_CHARS) + "]")
# 候选片段：字母数字段 + 1 个以上（分隔符 + 字母数字段）。
# **段数刻意不设上限**（批次 7 QA C-2）：原来写死 ``{1,6}``（最多 7 段），
# 遇到 ``8K0-615-001 8K0-615-002 8K0-615-003 8K0-615-004`` 就会断在件号
# 中间的 ``8K0`` 上，下一个匹配又从 ``615-003`` 开始，于是切出
# ``615-003 8K0-615-004`` 这种**半截畸形片段**，它居然能通过形状判定被当成
# 「合格件号」写进商机要点——展示错误件号比不展示更误导用户。去掉上限后
# 整条消息是一个完整候选，交给 :data:`_OE_SPLIT_RE` 切分时不会切在件号中间，
# 4 个件号一个都不会漏（只靠切分而不去掉上限，``…003`` 会丢）。
_OE_CAND_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9]{1,8}"
    r"(?:[" + re.escape(_OE_SEP_CHARS) + r"]{1,2}[A-Za-z0-9]{1,8})+"
    r"(?![A-Za-z0-9])")
# 日期形态（挡 ``2026-11-11`` / ``2026.11.11`` / ``2026/11/11``，以及全角减号
# 写法 ``2026－11－11``——不纳入的话它会被当「纯数字」去撞 A 类）
_OE_DATE_RE = re.compile(
    r"\d{4}[-./\u2010\u2011\u2012\u2013\uff0d]\d{1,2}"
    r"[-./\u2010\u2011\u2012\u2013\uff0d]\d{1,2}")
# 切分点：**空白 + 斜杠**（都是「两个件号之间」的天然分界）。
# ``-`` ``.`` ``·`` 和全角减号只在**件号内部**出现，绝不能当切分点，
# 否则 ``8K0-615-301`` 会被切成三段全丢；``/`` 反过来几乎不会出现在件号
# 内部（``8K0-615-301/4M0-615-301`` 是两个件号并排写），所以要切。
_OE_SPLIT_RE = re.compile("[ \u3000/]+")
# 候选原文的最大可能长度：合格形状紧凑长度 ≤17 位，分隔符每次 1~2 个字符、
# 最多 16 处 → 17 + 16*2 = 49。超过 49 的候选**不可能**通过形状判定，
# :func:`_oe_shape_kind` 一进门就 O(1) 拒掉，省掉几十万字符的 split/sub
# （批次 7 QA 性能项：``"a " * 50000`` 这种病态输入靠这一行压住大头）。
_OE_COMPACT_MAX = 17
_OE_SHAPE_MAX_RAW = _OE_COMPACT_MAX + 16 * 2
# VIN：17 位，排除 I / O / Q（与车辆识别代号标准一致）
_OE_VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")
# 每段长度上限 = 4：真实 OE 分段都在 4 以内（``8K0``/``615``/``301`` 各 3 位），
# 而 ``www.cpfly.top`` 的 ``cpfly`` = 5 位、``zhaoyu_4932403`` 的 ``zhaoyu`` = 6 位
_OE_SEG_MAX_LEN = 4
# 段数上限 = 6：``1K0 807 217 C`` 是 4 段，OE 最多见过 5~6 段（留 1 段余量）
_OE_SEG_MAX_COUNT = 6
# 纯数字 OE 位数区间 9~13：``313340-32080`` = 11 位、``42041-02870`` = 10 位，
# 下界 9 挡住 8 位日期串 ``20261111``，上界 13 与 OE 规则第 4 类一致
_OE_NUM_MIN, _OE_NUM_MAX = 9, 13
# 纯数字类里「所有段都 ≤3 位」= 小数串/递增序列，不是 OE：
# ``1-3-7-11-15-22-30``（刷单节奏）、``25.1 25.4 26.4 26.8``（股价小数）
_OE_SHORT_SEG = 3


def _oe_shape_kind(cand: str) -> str:
    """判断候选片段是否「像 OE」；返回类别（``num`` / ``seg`` / ``vin``），
    ``""`` 表示不接受。

    四类形状（批次 7 需求 1）：

    A. ``num``：紧凑后 9~13 位**纯数字**，不是日期形态，且**最长一段 > 3 位**
       （排除「每段都很短」的小数串/递增序列）。
       例：``313340-32080``（11 位）、``42041-02870``（10 位）。
    B. ``seg``：紧凑后 8~15 位，含数字、每段 ≤ :data:`_OE_SEG_MAX_LEN`、
       段数 ≤ :data:`_OE_SEG_MAX_COUNT`；再分两种：
       - 含**大写**字母的分段号（``8K0-615-301``、``4M0-615-301-A``、
         ``A000 420 26 20``、``1K0 807 217 C``）；
       - 全小写的分段号（``8k0-615-301``）额外要求段数 ≥3、至少一段字母
         数字混合、至少一段纯数字——这样 ``zhang.san``（无数字）、
         ``li.si-2024``（无混合段）、``www.cpfly.top``（无数字）都不命中。
    C. ``vin``：紧凑后恰好 17 位且符合 VIN 字符规则，**不受段长限制**
       （VIN 可能被写成 ``LSG-MJ5P57-8V016154``）。
    D. ``num``（短前缀 + 8 位件号）：``[短混合段][7~9 位纯数字段]``，
       如 ``V6 26345704``（通用件号是 8 位，原预设只认 10~13 位数字，原本漏）。

    Args:
        cand: :data:`_OE_CAND_RE` 匹配到的**候选原文片段**（保留分隔符）。

    Returns:
        ``"num"`` / ``"seg"`` / ``"vin"``；不接受时返回 ``""``。
    """
    text = str(cand or "")
    if len(text) > _OE_SHAPE_MAX_RAW:
        return ""                 # 原文都超长了，紧凑后必然是 >17 位，直接拒
    parts = _OE_SEP_RE.split(text)
    compact = _OE_SEP_RE.sub("", text)
    if not compact:
        return ""
    # C：被分隔符拆开的 VIN（17 位，不受段长限制）。
    # 必须**同时含字母和数字**：真实 VIN 的 WMI 段是字母、序号段是数字，而
    # 17 位**纯数字**（病态输入 ``"1-" * 20000`` 切出的 17 个 ``1``）或 17 位
    # 纯字母都不是 VIN，不能只靠字符集判定（批次 7 QA 复验发现的误命中）。
    if (len(compact) == 17 and _OE_VIN_RE.fullmatch(compact)
            and any(c.isalpha() for c in compact)
            and any(c.isdigit() for c in compact)):
        return "vin"
    # A：纯数字类（9~13 位、非日期形态、且不是「每段都很短」的序列）
    if compact.isdigit():
        if not (_OE_NUM_MIN <= len(compact) <= _OE_NUM_MAX):
            return ""
        if _OE_DATE_RE.search(text):
            return ""
        if max(len(p) for p in parts) <= _OE_SHORT_SEG:
            return ""
        return "num"
    if not (8 <= len(compact) <= 15):
        return ""
    if not any(c.isdigit() for c in compact):
        return ""
    # D：短混合前缀 + 7~9 位纯数字段（``V6 26345704`` 这种通用 8 位件号）
    if (len(parts) == 2 and len(parts[0]) <= _OE_SHORT_SEG
            and any(c.isalpha() for c in parts[0])
            and any(c.isdigit() for c in parts[0])
            and parts[1].isdigit() and 7 <= len(parts[1]) <= 9):
        return "num"
    if max(len(p) for p in parts) > _OE_SEG_MAX_LEN:
        return ""
    if len(parts) > _OE_SEG_MAX_COUNT:
        return ""
    # B1：含大写字母的分段号（8K0-615-301 / 4M0-615-301-A / A000 420 26 20）
    if any(c.isupper() for c in compact) and len(parts) >= 2:
        return "seg"
    # B2：全小写的分段号（8k0-615-301 / 4m0-615-301-a）：要求段数≥3、
    # 至少一段字母数字混合、至少一段纯数字（``li.si-2024``/``abc-123-def``
    # 没有混合段，``zhang.san`` 没有数字，``a100 h100`` 段数不足，均不命中）
    if (len(parts) >= 3
            and any(any(c.isalpha() for c in p) and any(c.isdigit() for c in p)
                    for p in parts)
            and any(p.isdigit() for p in parts)):
        return "seg"
    return ""


def _oe_seps_homogeneous(cand: str) -> bool:
    """候选内部的分隔符是否**同一种**（件号可信的必要条件）。

    一个件号内部通常只用同一种分隔符：``8K0-615-301`` 全用 ``-``、
    ``A000 420 26 20`` 全用空格、``LSG-MJ5P57-8V016154`` 全用 ``-``。
    出现「混用」几乎必然是**两个件号被粘在一起**（``8K0-615-301 4M0``、
    ``615-003 8K0-615-004``）——这种串即使能过 :func:`_oe_shape_kind` 的
    形状判定，也**不可信、不能展示**给用户（展示错误件号比不展示更误导）。

    Args:
        cand: 候选原文片段。

    Returns:
        ``True`` 表示 0~1 种分隔符（可信）；``False`` 表示混用（不可信）。
    """
    seps = {m.group(0) for m in re.finditer(
        "[" + re.escape(_OE_SEP_CHARS) + "]", str(cand or ""))}
    return len(seps) <= 1


def loose_oe_hits(text: str) -> List[str]:
    """宽松识别「不标准 OE」：返回**符合条件的候选原文片段**（去重、保序、≤10）。

    只扫描局部候选片段（:data:`_OE_CAND_RE`）+ 形状约束
    （:func:`_oe_shape_kind`），**不对全文做紧凑化**——全文紧凑化会把发送人
    用户名 / 网址 / 日期+时间粘成假的 8~15 位字母数字串（见本节顶部注释）。

    粘连切分兜底：拆车件群里「一条消息报多个件号、只用空格或 ``/`` 隔开」
    很常见（``8K0-615-301 4M0-615-301 有吗``、``8K0-615-301/4M0-615-301``）。
    候选正则本身允许用空格/横杠/``/`` 连接各段，于是相邻几个件号会被整段
    吃成一个超长候选（紧凑后 18~36 位 > 15 → 直接拒绝），真 OE 一起丢掉。
    所以**整段不通过形状判定时，按 :data:`_OE_SPLIT_RE`（空白 + 斜杠）切一层
    再逐段重判**（整段已通过的不切，``8K0 615 301`` 这种单个带空格的件号
    不会被切坏）。

    **已知限制（刻意不修）**：``8K0 615 301 8K0 615 302``——两个件号**都用
    空格**写且紧邻时仍识别不出，返回 ``[]``。原因是空格既是件号内部的分隔符
    （``8K0 615 301``、``1K0 807 217 C`` 都是这种写法），又是件号之间的天然
    分界；在没有更多上下文时，「两个 3 段件号」与「一个 6 段件号」在文本上
    无法区分，按空白切开后每段只剩 ``8K0``/``615``/``301``，单独看全都不合格。
    **这是刻意的取舍：宁可漏这一种少见写法，也不吐出错误件号**（商机要点里
    出现错误件号比不出现更误导用户）。未来若要支持，需要引入分词器/件号词典
    级别的上下文，不能靠正则堆规则。``/`` 是例外——它几乎只出现在件号之间
    （``8K0-615-301/4M0-615-301``），所以纳入了切分点。

    另：候选若内部**混用**不同分隔符（``8K0-615-301 4M0``），几乎必然是
    「两个件号被粘在一起」，由 :func:`_oe_seps_homogeneous` 拒掉，绝不整体展示。

    开销（批次 7 QA-P3-9，刻意不加窗口长度上限）：候选扫描覆盖整段文本、
    不设滑动窗口；合并文本一般只有几行到几十行，候选本身稀疏（要求 >=2 段
    且中间有分隔符），单次正则线性扫描，1800 条真实语料全量跑完 < 1 秒。
    切分分支对**不含空白/斜杠**的候选直接跳过（``_OE_SPLIT_RE.search`` 一行），
    :func:`_oe_shape_kind` 侧还有 :data:`_OE_SHAPE_MAX_RAW` 的 O(1) 早退，
    病态输入（``"1-" * 50000``）因此仍在几十毫秒内。结果上限由
    :func:`prescreen` / :func:`prescreen_hits` 侧的 10 条兜住。

    Args:
        text: 待匹配文本（合并后的分析文本）。

    Returns:
        命中的候选**原文片段**列表（保留原始分隔符，便于直接展示给用户），
        例：``["8K0-615-301", "4M0-615-301"]``。
    """
    raw = str(text or "")
    if not raw:
        return []
    out: List[str] = []
    try:
        matched = list(_OE_CAND_RE.finditer(raw))
    except re.error as exc:      # 理论上不会发生（正则常量固定）
        logger.warning("OE 候选正则异常（忽略）：%s", exc)
        return []
    for m in matched:
        cand = m.group(0)
        if _oe_shape_kind(cand) and _oe_seps_homogeneous(cand):
            if cand not in out:
                out.append(cand)
        else:
            # 粘连兜底：按空白/斜杠切一层逐段重判。不含空白和斜杠的候选切了
            # 也切不出东西（分隔符只剩 - . · 全角减号，属于件号内部），跳过。
            if not _OE_SPLIT_RE.search(cand):
                continue
            for piece in _OE_SPLIT_RE.split(cand):
                if (piece and _oe_shape_kind(piece)
                        and _oe_seps_homogeneous(piece)
                        and piece not in out):
                    out.append(piece)
                if len(out) >= 10:
                    break
        if len(out) >= 10:
            break
    return out


def prescreen_hits(text: str, keywords: str, regex: str) -> List[str]:
    """返回命中的筛选条件描述（关键词原文 / 正则匹配到的文本）。

    与 :func:`prescreen` 同一判定口径，供「不使用 AI 接口」模式把命中的
    关键词写进商机要点，让用户知道为什么被判为商机。

    原文正则命中不到的「不标准 OE」由 :func:`loose_oe_hits` 补充；这类命中
    写进返回值的格式是 ``"<候选原文片段>（宽松匹配）"``——用**原文片段**而不是
    紧凑串，用户才看得懂「为什么这条被判为商机」。标记写「宽松匹配」而不是
    「已去分隔符」：展示的是**保留分隔符**的原文片段，写「已去分隔符」会自相
    矛盾；而且短前缀件号类（``V6 26345704``）根本没去分隔符。原文命中的描述
    格式保持不变。
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
    # 宽松 OE 回退（只补「为什么命中」的描述，不改判定口径）
    if regex and len(hits) < 10:
        for cand in loose_oe_hits(text):
            if cand in hits:            # 原文已命中，不重复记
                continue
            label = f"{cand}（宽松匹配）"
            if label not in hits:
                hits.append(label)
            if len(hits) >= 10:
                break
    return hits[:10]


def prescreen(text: str, keywords: str, regex: str) -> bool:
    """轻量预筛：关键词（逗号分隔，中英文逗号均可）或正则任一命中即通过。

    关键词与正则均为空时视为全部通过（预筛不误伤）。

    正则未命中原文时，再用 :func:`loose_oe_hits` 做一次**宽松 OE 识别**：
    用户输入的 OE 经常不标准（``8K0-615-301``、``8K0 615 301``、
    ``1K0 807 217 C``、被分隔符拆开的 VIN），不能因为多打了个横杠/空格就
    丢掉商机。宽松识别只看**局部候选片段的形状**（段长/段数/是否含数字与
    大小写字母），**不对全文去分隔符**，因此不会把发送人用户名、网址、
    「日期 + 时间」误粘成 OE（详见本节顶部注释）。

    关键词只在**原文**里匹配，绝不碰候选/紧凑文本（否则「前杠」这类正常词
    会被粘坏）。
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
        # 宽松 OE 回退（候选片段 + 形状约束）
        if loose_oe_hits(text):
            return True
    return not kws and not regex


# ---------------------------------------------------------------------------
# openai SDK 客户端
# ---------------------------------------------------------------------------
def _is_unsupported_param_error(exc: BaseException) -> bool:
    """判断异常是否为「网关不认识 extra_body 参数」（用于关闭思考模式降级）。

    只在错误信息确实指向思考/附加参数时才判定，避免把 Key 无效、
    模型不存在等 400 误当作参数不支持而重复请求。

    兼容中文网关报错：GLM（智谱 bigmodel）等网关返回中文「参数错误」/
    「无效的参数」时同样判定为参数不支持，进入无参兜底重试。
    """
    text = str(exc).lower()
    tokens = ("thinking", "extra_body", "enable_thinking",
              "unexpected keyword", "unknown field", "unrecognized",
              "unsupported parameter", "extra fields",
              # 中文网关报错（GLM 等对 extra_body 直接回「参数错误」）
              "参数错误", "无效的参数", "invalid parameters")
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
    system_prompt = build_system_prompt(types, strictness, examples=examples,
                                        group_chat=(session_kind == "group"))
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
