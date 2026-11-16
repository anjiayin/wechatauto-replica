# -*- coding: utf-8 -*-
"""发送人显示名解析（批次 11 需求 2）：群昵称 > 联系人昵称 > wxid。

解析优先级（用户口径：显示出来的名字要和微信里看到的一致）：

1. ``group_cards``（群 + 成员 → 群名片）：微信 4.x 本地库不存群名片，
   群名片是消息到达时由 session.db 的 ``last_sender_display_name`` 学习
   落库的（批次 5 需求 ②），因此这一层最准；
2. ``WeChatService.get_display_name(wxid)``：联系人昵称/备注
   （服务内部已有 ``_name_cache``，不会再重复打微信库）；
3. 回退 wxid 本身（解析不出来也必须显示点东西）。

性能约束：日志页一页 20 行，「发送人」筛选的候选最多 500 条，逐行查
``group_cards`` 会拖垮接口。因此群名片按 ``chatroom`` 做进程内缓存
（带 TTL 与容量上限）——一页日志通常只涉及 1~3 个群，命中率极高。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, Tuple

from app.db import Database

logger = logging.getLogger(__name__)

# 群名片缓存：chatroom → (写入时刻, {member: card})
_CARD_CACHE: "OrderedDict[str, Tuple[float, Dict[str, str]]]" = OrderedDict()
_CARD_CACHE_LOCK = threading.Lock()
# 名片随时可能被成员改掉，但新消息到达时会持续覆盖更新本表；
# 缓存 TTL 只需保证「改完之后秒级内不再读到旧值」即可。
_CARD_CACHE_TTL_SEC = 120.0
# 上限保护：群很多时不能无限增长
_CARD_CACHE_MAX = 256

# 显示名解析器类型：wxid → 显示名（失败可返回空串）
NameResolver = Callable[[str], str]


def _load_group_cards(db: Database, chatroom: str) -> Dict[str, str]:
    """取一个群的名片表（带进程内缓存，DB 异常时返回空表不抛）。

    Args:
        db: 业务库实例（提供 ``get_group_cards``）。
        chatroom: 群 username（``xxx@chatroom``）。

    Returns:
        ``{member_wxid: card}``；读库失败返回空表（只是显示名退化，
        绝不能让日志列表 500）。
    """
    now = time.monotonic()
    with _CARD_CACHE_LOCK:
        hit = _CARD_CACHE.get(chatroom)
        if hit and (now - hit[0]) < _CARD_CACHE_TTL_SEC:
            _CARD_CACHE.move_to_end(chatroom)   # LRU：命中即刷新新鲜度
            return hit[1]
    try:
        cards = db.get_group_cards(chatroom) or {}
    except Exception:
        logger.warning("读取群名片失败（显示名回退联系人昵称）：%s", chatroom)
        cards = {}
    with _CARD_CACHE_LOCK:
        _CARD_CACHE[chatroom] = (now, cards)
        _CARD_CACHE.move_to_end(chatroom)
        while len(_CARD_CACHE) > _CARD_CACHE_MAX:
            _CARD_CACHE.popitem(last=False)     # 淘汰最久未用
    return cards


def resolve_wechat_id(svc: Any, username: str) -> str:
    """username → 微信号（contact.alias），供各路由统一补齐展示字段。

    用户要求界面上原 wxid 的位置统一改显微信号。本函数是**唯一**的路由侧
    安全包装：委托 ``svc.get_wechat_id``（内部 alias 优先、备注/昵称回退、
    带进程内缓存），并在服务替身缺方法/读库失败时返回空串——微信号只是
    显示增强，绝不能让商机/日志/设置页 500。

    Args:
        svc: 微信服务实例（可能为 None / 测试替身）。
        username: 联系人/会话 username。

    Returns:
        微信号（或备注/昵称回退值）；解析不到返回空串。
    """
    name = str(username or "")
    if not name or svc is None:
        return ""
    getter = getattr(svc, "get_wechat_id", None)
    if not callable(getter):
        return ""
    try:
        return str(getter(name) or "")
    except Exception:
        logger.debug("读取微信号失败（展示留空）：%s", name)
        return ""


def clear_cache() -> None:
    """清空群名片缓存（测试用；名片刚学习完想立刻看到新名字时也可调用）。"""
    with _CARD_CACHE_LOCK:
        _CARD_CACHE.clear()


def resolve_sender_display(db: Database, name_resolver: NameResolver,
                           chatroom: str, sender_wxid: str) -> str:
    """解析一条日志/商机的发送人显示名。

    优先级：群名片（仅群会话）→ 联系人昵称 → wxid 本身。任何一层失败都
    静默降级到下一层，绝不抛异常（日志页不能因为某个名字解析失败整页 500）。

    Args:
        db: 业务库实例（群名片来源）。
        name_resolver: 联系人昵称解析函数（通常是 ``svc.get_display_name``）。
        chatroom: 会话 username（群会话才查群名片，私聊直接用联系人昵称）。
        sender_wxid: 发送人 wxid。

    Returns:
        显示名；``sender_wxid`` 为空时返回空串（前端据此不展示该行）。
    """
    wxid = str(sender_wxid or "").strip()
    if not wxid:
        return ""
    room = str(chatroom or "").strip()
    if room.endswith("@chatroom"):
        card = str(_load_group_cards(db, room).get(wxid) or "").strip()
        if card:
            return card
    name = ""
    if name_resolver is not None:
        try:
            name = str(name_resolver(wxid) or "").strip()
        except Exception:
            logger.warning("解析联系人昵称失败（回退 wxid）：%s", wxid)
            name = ""
    return name or wxid
