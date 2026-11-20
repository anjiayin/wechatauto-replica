# -*- coding: utf-8 -*-
"""群消息总结服务层（纯逻辑，无 HTTP、无全局单例）。

依赖全部注入：Database（落库/设置/账单）、WeChatService（按时间范围取消息与
取名链）、client_factory（AIClient 工厂，测试可注入替身）。API 路由只负责
参数校验与异常翻译，本模块可独立单测。

时间口径：业务库 TEXT 为 ``YYYY-MM-DD HH:MM:SS``；微信消息 create_time 为
epoch 秒；SQL 内只做整数比较，绝不用 Python 拼 T 分隔字符串入 SQL。
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Callable, Optional

from app import analyzer
from app.billing import estimate_cost, get_billing_settings
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
    SUMMARY_SCHEDULE_TIME_DEFAULT,
)
from app.crypto import decrypt_api_key
from app.db import Database, read_ai_settings
from app.schemas import STYLE_VALUES

logger = logging.getLogger(__name__)

# 语料预算：中文 1 字符≈1 token 量级；六节输出的固定估值。
EST_OUTPUT_TOKENS = 2000
SUMMARY_MSG_LIMIT = 5000

# 四处统一文案：结果区 / txt / html / AI 语料首行都用 truncation_text() 生成。
TRUNCATION_TEXT = "已截断：仅统计最近 {n} 条"
BUDGET_HINT_NO_PRICE = "请先在设置页配置模型单价，否则金额预算不生效"
OVER_BUDGET_DETAIL = "预估 {est} 已超预算 {budget}，请收窄范围或调整设置页预算/策略"
OVER_BUDGET_UNREACHABLE = "无法在预算内生成，请收窄时间范围或调整预算设置"

STYLE_RULES: dict[str, str] = {
    "detailed": "每节 4-6 条详述，保留关键细节、时间与成员姓名。",
    "concise": "每节 2-3 条短句，只保留核心结论。",
    "bullets": "每节给出短语级要点，不写完整段落。",
}

SECTION_FIELDS: tuple[tuple[str, str], ...] = (
    ("reminders", "重要提醒"),
    ("topics", "热门话题"),
    ("comments", "点评"),
    ("followups", "待跟进事项"),
    ("others", "其他讨论"),
    ("closing", "结语"),
)

# System Prompt 模板：JSON 示例的花括号必须转义（str.format 注入风格/补充要求）。
SYSTEM_PROMPT_TEMPLATE = """你是微信群聊记录总结助手。请阅读下面的群聊记录，按要求输出固定六个部分的总结。
## 输出 JSON 格式
{{\"reminders\": <string[]>, \"topics\": <string[]>, \"comments\": <string[]>, \"followups\": <string[]>, \"others\": <string[]>, \"closing\": <string>}}
## 各部分定义
- reminders 重要提醒：需立即关注的事项（投诉、故障、紧急需求、明确商机线索）
- topics 热门话题：被反复讨论的主题，注明涉及成员
- comments 点评：对讨论内容的客观分析判断
- followups 待跟进事项：需后续跟进的具体行动项，必须注明相关成员名
- others 其他讨论：值得留意的其余内容
- closing 结语：一段话总览（不列点）
## 风格要求
{style_rules}
{custom_block}
## 原则
1. 只基于聊天记录，不臆测；发言者名称以记录中的显示名为准。
2. 空节输出空数组 []，不要编造。
3. 只输出一个 JSON 对象，不要输出其他文字或 Markdown 代码块标记。
"""

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


class SummaryOverBudgetError(Exception):
    """超预算且策略为 block（或截断后仍超预算）时抛出，API 映射 413。"""


def truncation_text(n: int, original: int = 0) -> str:
    """统一截断标注文案（可附原共 M 条，主文案不得改写）。"""
    text = TRUNCATION_TEXT.format(n=int(n))
    if int(original or 0) > 0:
        text += f"（原共 {int(original)} 条）"
    return text


def parse_time_range(start: str, end: str) -> tuple[datetime, datetime, int, int]:
    """校验并解析时间范围，返回 (start_dt, end_dt, start_epoch, end_epoch)。

    只允许 ``YYYY-MM-DD HH:MM:SS``（前端 datetime-local 的 T 在提交前转换）。
    """
    try:
        start_dt = datetime.strptime(str(start), "%Y-%m-%d %H:%M:%S")
        end_dt = datetime.strptime(str(end), "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError) as exc:
        raise ValueError("时间格式应为 YYYY-MM-DD HH:MM:SS") from exc
    if start_dt > end_dt:
        raise ValueError("开始时间不能晚于结束时间")
    return start_dt, end_dt, int(start_dt.timestamp()), int(end_dt.timestamp())


def build_system_prompt(style: str, custom_prompt: str = "") -> str:
    """按风格 + 用户补充要求构造 System Prompt（花括号已转义）。"""
    if style not in STYLE_RULES:
        raise ValueError(f"不支持的总结风格：{style}")
    custom_block = ""
    if str(custom_prompt or "").strip():
        custom_block = ("## 补充要求（用户指定，优先满足）\n"
                        + str(custom_prompt).strip())
    return SYSTEM_PROMPT_TEMPLATE.format(
        style_rules=STYLE_RULES[style],
        custom_block=custom_block,
    )


def build_user_message(session_name: str, start: str, end: str,
                       member_names: list[str], msg_count: int, corpus: str,
                       truncated: int = 0, truncated_original: int = 0) -> str:
    """构造 User Message（含成员筛选与截断提示）。"""
    member_text = "全部" if not member_names else (
        f"仅 {len(member_names)} 人：{'、'.join(member_names)}")
    parts = [
        f"群聊：{session_name}",
        f"时间范围：{start} ~ {end}",
        f"成员筛选：{member_text}",
    ]
    parts.append(f"消息（共 {msg_count} 条，按时间正序）：")
    # 统一文案由 build_corpus 放在语料首行；仅当调用方传入的语料未带标注时
    # 才在此补一行，避免同一份 User Message 里出现两次截断提示。
    if truncated:
        notice = truncation_text(truncated, truncated_original)
        if not corpus.startswith(notice):
            parts.append(notice)
    parts.append(corpus)
    return "\n".join(parts)


def _corpus_line(msg: dict) -> str:
    """单条语料行 ``[MM-DD HH:MM] 显示名: 内容``。"""
    create_time = str(msg.get("create_time") or "")
    stamp = create_time[5:16].replace("T", " ")
    if not re.match(r"^\d{2}-\d{2} \d{2}:\d{2}$", stamp):
        stamp = "--"
    name = str(msg.get("sender_name") or msg.get("sender_wxid") or "未知用户")
    content = str(msg.get("content") or "").replace("\n", " ").strip()
    return f"[{stamp}] {name}: {content}"


def _stats_from_msgs(msgs: list[dict]) -> dict:
    """按消息列表统计字符量/条数/发言人数与预算估算输入。"""
    senders = {str(m.get("sender_wxid") or "") for m in msgs}
    char_count = sum(len(str(m.get("content") or "")) for m in msgs)
    return {
        "msg_count": len(msgs),
        "char_count": char_count,
        "member_count": len(senders),
        "est_prompt_tokens": char_count,
        "est_output_tokens": EST_OUTPUT_TOKENS,
        "est_tokens": char_count + EST_OUTPUT_TOKENS,
    }


def parse_summary_json(text: str) -> dict:
    """剥代码块 → json.loads → 正则兜底 → 规整；失败抛 AIError。"""
    raw = str(text or "").strip()
    block = _JSON_BLOCK_RE.search(raw)
    if block:
        raw = block.group(1).strip()
    data: Any = None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        match = _JSON_OBJECT_RE.search(raw)
        if match:
            try:
                data = json.loads(match.group(0))
            except (ValueError, TypeError):
                data = None
    if not isinstance(data, dict):
        raise analyzer.AIError("AI 返回内容不是合法的 JSON 总结")
    out: dict[str, Any] = {}
    for key, _label in SECTION_FIELDS:
        if key == "closing":
            value = data.get("closing", "")
            if isinstance(value, list):
                value = " ".join(str(x) for x in value if str(x or "").strip())
            out[key] = str(value or "").strip()
            continue
        value = data.get(key, [])
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            value = []
        out[key] = [str(x).strip() for x in value if str(x or "").strip()]
    return out


class SummaryService:
    """群消息总结核心服务（三处依赖全注入）。"""

    # 兼容类接口名（类图：SummaryService._parse_summary_json）
    _parse_summary_json = staticmethod(parse_summary_json)

    def __init__(self, db: Database, wechat: Any,
                 client_factory: Optional[Callable] = None) -> None:
        self._db = db
        self._wechat = wechat
        self._client_factory = client_factory or self._default_client_factory

    # ------------------------------------------------------------------
    # AI 客户端（与 Pipeline._build_client 同构）
    # ------------------------------------------------------------------
    @staticmethod
    def _default_client_factory(cfg: dict):
        """默认 AIClient 工厂（关闭思考模式）。"""
        return analyzer.AIClient(
            base_url=cfg["base_url"], api_key=cfg["api_key"],
            model=cfg["model"], timeout_sec=cfg["timeout_sec"],
            temperature=cfg["temperature"],
            disable_thinking=bool(cfg.get("disable_thinking", True)),
        )

    @staticmethod
    def _thinking_flag(raw: object, default: bool = True) -> bool:
        """解析 disable_thinking 配置（与 Pipeline 同口径）。"""
        if raw is None or raw == "":
            return default
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() not in ("0", "false", "no", "off", "")

    def _build_client(self, ai_cfg: dict, api_key: str):
        """按配置构建 AI 客户端（非法数值时回退默认值，与 Pipeline 一致）。"""
        disable_thinking = self._thinking_flag(ai_cfg.get("disable_thinking"))
        base = {
            "base_url": ai_cfg.get("base_url", ""),
            "api_key": api_key,
            "model": ai_cfg.get("model", ""),
            "timeout_sec": int(float(ai_cfg.get("timeout_sec", "60"))),
            "temperature": float(ai_cfg.get("temperature", "0.2")),
            "disable_thinking": disable_thinking,
        }
        try:
            return self._client_factory(base)
        except (TypeError, ValueError):
            base["timeout_sec"] = 60
            base["temperature"] = 0.2
            return self._client_factory(base)

    # ------------------------------------------------------------------
    # 语料 / 预算
    # ------------------------------------------------------------------
    @staticmethod
    def build_corpus(msgs: list[dict], senders: Optional[list[str]] = None,
                     truncated_original: int = 0) -> tuple[str, dict]:
        """成员筛选、语料行组装与统计；返回 (corpus, stats)。

        senders 为空表示全部成员；truncated_original>0 时首行注入统一截断文案。
        """
        selected = set(str(s) for s in (senders or []) if str(s or ""))
        filtered = [m for m in msgs
                    if not selected or str(m.get("sender_wxid") or "") in selected]
        lines = []
        if truncated_original:
            lines.append(truncation_text(len(filtered), truncated_original))
        lines.extend(_corpus_line(m) for m in filtered)
        return "\n".join(lines), _stats_from_msgs(filtered)

    @staticmethod
    def _budget_value(db: Database, mode: str) -> float:
        """读取当前口径的预算值（非法/未配置返回 0 = 不超预算）。"""
        key = KEY_SUMMARY_BUDGET_MONEY if mode == "money" \
            else KEY_SUMMARY_BUDGET_TOKENS
        try:
            value = db.get_float(key, 1.0 if mode == "money" else 60000.0)
        except (TypeError, ValueError):
            value = 0.0
        return max(0.0, float(value or 0.0))

    @staticmethod
    def _format_budget(mode: str, value: float) -> str:
        """预算展示文案（金额 ¥X.XX / token 数带千分位）。"""
        if mode == "money":
            return f"¥{value:.2f}"
        return f"{int(value):,} tokens"

    def check_budget(self, stats: dict) -> dict:
        """预算预检：双口径换算、占比、超预算判断与固定提示文案。"""
        billing = get_billing_settings(self._db)
        mode = self._db.get_setting(KEY_SUMMARY_BUDGET_MODE, "money")
        if mode not in BUDGET_MODE_VALUES:
            mode = "money"
        action = self._db.get_setting(KEY_SUMMARY_OVER_LIMIT_ACTION, "block")
        if action not in OVER_LIMIT_ACTION_VALUES:
            action = "block"
        try:
            truncate_n = max(1, int(self._db.get_int(KEY_SUMMARY_TRUNCATE_RECENT_N, 200)))
        except (TypeError, ValueError):
            truncate_n = 200
        budget_value = self._budget_value(self._db, mode)
        pt = int(stats.get("est_prompt_tokens") or 0)
        ct = int(stats.get("est_output_tokens") or EST_OUTPUT_TOKENS)
        est_tokens = pt + ct
        est_cost = estimate_cost(pt, ct, billing)
        est = est_cost if mode == "money" else float(est_tokens)
        if budget_value <= 0:
            ratio = 0.0
            over = False
        else:
            ratio = est / budget_value
            over = est > budget_value
        budget_hint = ""
        if mode == "money" and not billing["input_price"] and not billing["output_price"]:
            budget_hint = BUDGET_HINT_NO_PRICE
        return {
            "est_prompt_tokens": pt,
            "est_output_tokens": ct,
            "est_tokens": est_tokens,
            "est_cost": est_cost,
            "budget_mode": mode,
            "budget_value": budget_value,
            "budget_text": self._format_budget(mode, budget_value),
            "usage_ratio": ratio,
            "over_budget": over,
            "action": action,
            "truncate_n": truncate_n,
            "budget_hint": budget_hint,
        }

    def apply_truncate(self, msgs: list[dict],
                       stats: dict) -> tuple[list[dict], dict, int]:
        """超预算时取最近 N 条（按 10% 递减），返回 (kept, stats, original)。

        N<10 仍超预算时抛 SummaryOverBudgetError。
        """
        budget = self.check_budget(stats)
        current = max(1, min(budget["truncate_n"], len(msgs)))
        original = len(msgs)
        while True:
            kept = msgs[-current:]
            new_stats = _stats_from_msgs(kept)
            if not self.check_budget(new_stats)["over_budget"]:
                return kept, new_stats, original
            if current < 10:
                raise SummaryOverBudgetError(OVER_BUDGET_UNREACHABLE)
            current = max(1, int(current * 0.9))

    # ------------------------------------------------------------------
    # 查询与生成
    # ------------------------------------------------------------------
    def _session_name(self, session: str) -> str:
        """群显示名（走现有取名链，失败回退 username）。"""
        getter = getattr(self._wechat, "display_name_with_corp", None)
        if getter is None:
            getter = getattr(self._wechat, "get_display_name", None)
        if getter is None:
            return session
        try:
            return str(getter(session) or "") or session
        except Exception:
            return session

    def _fetch(self, session: str, start: str, end: str) -> tuple[list[dict], str]:
        """校验时间范围并按 epoch 秒取消息。"""
        _start_dt, _end_dt, start_epoch, end_epoch = parse_time_range(start, end)
        msgs, error = self._wechat.get_messages_in_range(
            session, start_epoch, end_epoch, limit=SUMMARY_MSG_LIMIT)
        return list(msgs or []), str(error or "")

    @staticmethod
    def _filter(msgs: list[dict], senders: Optional[list[str]]) -> list[dict]:
        """按 sender_wxid 筛选（空 = 全部）。"""
        selected = set(str(s) for s in (senders or []) if str(s or ""))
        if not selected:
            return list(msgs)
        return [m for m in msgs
                if str(m.get("sender_wxid") or "") in selected]

    def members(self, session: str, start: str, end: str,
                senders: Optional[list[str]] = None) -> dict:
        """全量成员列表 + 筛选后统计与预算字段。"""
        msgs, error = self._fetch(session, start, end)
        items: list[dict] = []
        grouped: dict[str, dict] = {}
        for msg in msgs:
            wxid = str(msg.get("sender_wxid") or "")
            if not wxid:
                continue
            row = grouped.setdefault(wxid, {
                "sender_wxid": wxid, "display_name": "",
                "count": 0, "chars": 0,
            })
            row["count"] += 1
            row["chars"] += len(str(msg.get("content") or ""))
            if not row["display_name"]:
                row["display_name"] = str(msg.get("sender_name") or "")
        for wxid, row in grouped.items():
            resolver = getattr(self._wechat, "resolve_group_member_display", None)
            display = ""
            if resolver is not None:
                try:
                    display = str(resolver(session, wxid) or "")
                except Exception:
                    display = ""
            # 取名链解析不出来（返回空或原 wxid）时回退消息行显示名，
            # 成员下拉不该把内部 wxid 当成人名展示。
            if not display or display == wxid:
                display = row["display_name"] or wxid
            items.append({
                "sender_wxid": wxid,
                "display_name": display,
                "count": row["count"],
                "chars": row["chars"],
            })
        items.sort(key=lambda x: (-x["count"], x["display_name"]))
        filtered = self._filter(msgs, senders)
        stats = _stats_from_msgs(filtered)
        budget = self.check_budget(stats)
        return {
            "items": items,
            "msg_count": stats["msg_count"],
            "char_count": stats["char_count"],
            "error": error,
            **budget,
        }

    def generate(self, session: str, start: str, end: str,
                 senders: Optional[list[str]] = None,
                 style: str = "detailed",
                 custom_prompt: str = "") -> dict:
        """完整生成链路：校验 → 语料 → 预算 → AI → 解析 → 双写落库。"""
        if style not in STYLE_VALUES:
            raise ValueError(f"不支持的总结风格：{style}")
        if len(str(custom_prompt or "")) > 500:
            raise ValueError("自定义提示词不能超过 500 字")
        msgs, error = self._fetch(session, start, end)
        if error:
            raise RuntimeError(error)
        if len(msgs) >= SUMMARY_MSG_LIMIT:
            raise SummaryOverBudgetError(
                f"数据过多（单次最多 {SUMMARY_MSG_LIMIT} 条），请收窄时间范围后重试")
        if not msgs:
            raise ValueError("所选时间范围内没有消息")
        member_count = len({str(m.get("sender_wxid") or "") for m in msgs})
        filtered = self._filter(msgs, senders)
        if not filtered:
            raise ValueError("所选成员在该时间范围内没有发言")
        corpus, stats = self.build_corpus(filtered, senders=[])
        budget = self.check_budget(stats)
        truncated = False
        truncated_original = 0
        if budget["over_budget"]:
            if budget["action"] == "block":
                raise SummaryOverBudgetError(OVER_BUDGET_DETAIL.format(
                    est=budget["est_cost"] if budget["budget_mode"] == "money"
                    else f"{budget['est_tokens']:,} tokens",
                    budget=budget["budget_text"]))
            filtered, stats, truncated_original = self.apply_truncate(
                filtered, stats)
            corpus, stats = self.build_corpus(
                filtered, senders=[], truncated_original=truncated_original)
            truncated = True
        name_map: dict[str, str] = {}
        for msg in msgs:
            wxid = str(msg.get("sender_wxid") or "")
            if wxid and wxid not in name_map:
                name_map[wxid] = str(msg.get("sender_name") or "") or wxid
        if senders:
            selected_names = [name_map.get(str(s), str(s)) for s in senders]
        else:
            selected_names = list(name_map.values())
        user_message = build_user_message(
            self._session_name(session), start, end, selected_names,
            stats["msg_count"], corpus,
            # 语料首行（build_corpus）已注入统一截断文案，这里不再重复一次。
            truncated=0,
            truncated_original=truncated_original,
        )
        ai_cfg = read_ai_settings(self._db)
        api_key = decrypt_api_key(ai_cfg.get("api_key_enc", ""))
        if not ai_cfg.get("base_url") or not api_key or not ai_cfg.get("model"):
            raise analyzer.AIError("AI 接口未配置完整，请在设置页配置")
        client = self._build_client(ai_cfg, api_key)
        started = time.monotonic()
        raw = client.chat(build_system_prompt(style, custom_prompt), user_message)
        latency_ms = int((time.monotonic() - started) * 1000)
        content = parse_summary_json(raw)
        usage = getattr(client, "last_usage", None) or {}
        pt = usage.get("prompt_tokens")
        ct = usage.get("completion_tokens")
        pt = int(pt) if pt is not None else None
        ct = int(ct) if ct is not None else None
        row = self._db.insert_group_summary(
            session_username=session, session_name=self._session_name(session),
            time_start=start, time_end=end, msg_count=stats["msg_count"],
            member_count=member_count,
            sender_wxids=json.dumps(list(senders or []), ensure_ascii=False),
            member_names=json.dumps(selected_names, ensure_ascii=False),
            style=style, custom_prompt=str(custom_prompt or ""),
            content=json.dumps(content, ensure_ascii=False),
            truncated=truncated, truncated_original=truncated_original,
            model=str(ai_cfg.get("model") or ""), latency_ms=latency_ms,
            prompt_tokens=pt, completion_tokens=ct,
        )
        if pt is not None or ct is not None:
            self._db.insert_ai_usage(
                prompt_tokens=pt or 0, completion_tokens=ct or 0,
                model=str(ai_cfg.get("model") or ""), session_username=session)
        row["truncated"] = bool(row.get("truncated"))
        return row

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    @staticmethod
    def _sections(row: dict) -> dict[str, Any]:
        """安全读取 content JSON，缺节按空值处理。"""
        try:
            data = json.loads(str(row.get("content") or "{}"))
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        return data

    def render_txt(self, row: dict) -> str:
        """纯文本导出（参数摘要 + 六节 + 统一截断文案）。"""
        lines = [
            "群消息总结",
            f"群聊：{row.get('session_name') or row.get('session_username') or ''}",
            f"时间范围：{row.get('time_start', '')} ~ {row.get('time_end', '')}",
            f"生成时间：{row.get('created_at', '')}",
            f"风格：{row.get('style', '')} · 消息数：{row.get('msg_count', 0)}"
            f" · 成员数：{row.get('member_count', 0)}",
        ]
        if row.get("truncated"):
            lines.append(truncation_text(
                row.get("msg_count", 0), row.get("truncated_original", 0)))
        data = self._sections(row)
        for index, (key, label) in enumerate(SECTION_FIELDS, start=1):
            lines.append("")
            lines.append(f"{index}、{label}")
            value = data.get(key, "")
            if key == "closing":
                lines.append(str(value or "（无）"))
            else:
                items = value if isinstance(value, list) else []
                lines.extend(f"- {x}" for x in items if str(x or "").strip())
                if not items:
                    lines.append("（无）")
        return "\n".join(lines) + "\n"

    def render_html(self, row: dict) -> str:
        """HTML 导出（单文件、内联浅色样式 + @media print，动态文本转义）。"""
        def esc(value: Any) -> str:
            return html.escape(str(value or ""), quote=True)

        title = esc(f"群消息总结-{row.get('session_name') or row.get('session_username') or ''}")
        meta = [
            f"群聊：{esc(row.get('session_name') or row.get('session_username') or '')}",
            f"时间范围：{esc(row.get('time_start', ''))} ~ {esc(row.get('time_end', ''))}",
            f"生成时间：{esc(row.get('created_at', ''))}",
            f"风格：{esc(row.get('style', ''))} · "
            f"消息数：{esc(row.get('msg_count', 0))} · "
            f"成员数：{esc(row.get('member_count', 0))}",
        ]
        if row.get("truncated"):
            meta.append(
                '<div class="truncate">'
                + esc(truncation_text(row.get("msg_count", 0),
                                      row.get("truncated_original", 0)))
                + "</div>")
        data = self._sections(row)
        parts = []
        for key, label in SECTION_FIELDS:
            value = data.get(key, "")
            if key == "closing":
                body = f'<p>{esc(value) or "（无）"}</p>'
            else:
                items = value if isinstance(value, list) else []
                body = ("<ul>" + "".join(
                    f"<li>{esc(x)}</li>" for x in items if str(x or "").strip())
                    + "</ul>") if items else "<p>（无）</p>"
            parts.append(f"<section><h2>{esc(label)}</h2>{body}</section>")
        return (
            "<!DOCTYPE html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<title>{title}</title><style>"
            "body{font-family:'Microsoft YaHei',Arial,sans-serif;color:#24292b;"
            "background:#fff;line-height:1.7;margin:32px auto;max-width:860px;"
            "padding:0 20px}h1{font-size:22px;border-bottom:2px solid #2e7d32;"
            "padding-bottom:8px}h2{font-size:16px;color:#1b5e20;margin:20px 0 6px}"
            ".meta{background:#f5f8f5;border:1px solid #d7e6d8;border-radius:6px;"
            "padding:10px 14px;font-size:13px}.truncate{color:#a3231d;"
            "font-weight:700;margin-top:6px}section{break-inside:avoid}"
            "ul{margin:4px 0 4px 20px;padding:0}li{margin:3px 0}p{margin:4px 0}"
            "@media print{body{margin:0;max-width:none;padding:0;font-size:12px}"
            "section{page-break-inside:avoid}h2{color:#000}}"
            "</style></head><body>"
            f"<h1>{title}</h1>"
            f"<div class=\"meta\"><div>{'</div><div>'.join(meta)}</div></div>"
            + "".join(parts)
            + "</body></html>\n"
        )


class SummaryScheduler:
    """P1 定时总结调度器骨架（T05 接入 main.py 与 WS 通知后启用）。

    P0 阶段不启动任何后台线程、不产生定时清理或发送行为；配置读取与命中
    判断集中在这里，T05 只需在 tick() 中调用 SummaryService.generate。
    """

    def __init__(self, service: SummaryService, notifier: Any = None) -> None:
        self._service = service
        self._notifier = notifier
        self._started = False

    def start(self) -> None:
        """标记已启动（P0 不拉起线程；T05 在此创建 daemon 线程）。"""
        self._started = True
        logger.info("群消息总结定时调度器已标记启动（P0 未启用后台线程）")

    def stop(self) -> None:
        """停止标记（P0 无线程可停）。"""
        self._started = False

    def tick(self) -> list[dict]:
        """返回当前已启用且触发时间命中 HH:MM 的按群配置（P0 只检查不生成）。"""
        db = self._service._db
        if db.get_setting(KEY_SUMMARY_SCHEDULE_ENABLED, "0") != "1":
            return []
        try:
            configs = json.loads(db.get_setting(KEY_SUMMARY_SCHEDULE_CONFIGS, "[]"))
        except (TypeError, ValueError):
            return []
        if not isinstance(configs, list):
            return []
        now_hm = datetime.now().strftime("%H:%M")
        return [c for c in configs
                if isinstance(c, dict) and c.get("enabled")
                and str(c.get("time") or SUMMARY_SCHEDULE_TIME_DEFAULT) == now_hm]

    @property
    def started(self) -> bool:
        """调度器是否已标记启动（T05 线程生命周期检查用）。"""
        return self._started
