# -*- coding: utf-8 -*-
"""非文本消息内容友好化：把 XML/占位内容转为人可读的短文本。

微信 4.x 数据库里，链接/小程序/文件/位置/动画表情等消息的
``message_content`` 是原始 XML 或 ``[类型]`` 占位串，直接展示对人不
友好（反馈清单第 3 项）。本模块按类型解析 XML 提取标题/描述/链接等
关键字段，输出形如::

    [链接] 标题
    https://example.com/xxx
    [小程序] 某某小程序卡片
    [图片]
    [动画表情]

解析失败一律回退类型占位（如 ``[链接]``），任何场景不透传原始 XML。
仅用于展示层与日志；AI 分析输入保持原样（文本消息不受影响）。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Optional

# XML 转义值里常见的 &amp; 等实体由 ElementTree 自动处理
_TAG_RE = re.compile(r"<[^>]+>")


def _xml_parse(text: str) -> Optional[ET.Element]:
    """容错解析消息 XML；失败返回 None。"""
    try:
        return ET.fromstring(text)
    except Exception:
        return None


def _find_text(root: ET.Element, tag: str) -> str:
    """取指定子标签的文本（strip 后；缺失返回空串）。"""
    node = root.find(tag)
    if node is not None and node.text:
        return node.text.strip()
    return ""


def _format_appmsg(content: str, type_name: str) -> str:
    """type 49 系消息（appmsg）：链接/文件/小程序/引用等。"""
    root = _xml_parse(content)
    if root is None:
        # XML 解析失败：可能是被截断的半截 XML，回退类型占位
        return f"[{type_name}]"
    app_node = root.find("appmsg") if root.find("appmsg") is not None else root
    def _t(tag: str) -> str:
        node = app_node.find(tag)
        return node.text.strip() if node is not None and node.text else ""
    title = _t("title")
    des = _t("des")
    url = _t("url")
    # type 可能是 appmsg 的属性（<appmsg type="33">）或子元素
    app_type = (app_node.get("type") or _t("type") or "").strip()
    if title.startswith("<?xml") or "<" in title:
        title = ""  # 脏数据防御：标题里又嵌 XML 时弃用
    kind = "链接"
    if app_type in ("33", "36"):
        kind = "小程序"
    elif app_type == "6":
        kind = "文件"
    elif app_type == "57" or title.startswith("[引用"):
        kind = "引用消息"
    elif app_type == "19":
        kind = "合并转发"
    head = f"[{kind}] {title}".rstrip() if title else f"[{kind}]"
    lines = [head]
    if des and des != title:
        lines.append(des[:200])
    if url and kind in ("链接", "小程序"):
        lines.append(url[:300])
    return "\n".join(lines)


def format_content(type_name: str, content: str) -> str:
    """按消息类型把原始内容转为对人友好的展示文本。

    Args:
        type_name: 消息类型名（如 图片 / 动画表情 / 文件、链接、卡片）。
        content: 原始 message_content。

    Returns:
        友好展示文本；文本消息与无法识别的类型原样返回。
    """
    content = str(content or "")
    tname = str(type_name or "")
    if tname == "文本" or not content:
        return content
    if tname == "图片":
        return "[图片]"
    if tname == "动画表情":
        return "[动画表情]"
    if tname == "语音":
        return "[语音]"
    if tname == "视频":
        return "[视频]"
    if tname == "位置":
        root = _xml_parse(content)
        if root is not None:
            poiname = _find_text(root, "location/poiname") \
                or _find_text(root, "location/label")
            if poiname:
                return f"[位置] {poiname}"
        return "[位置]"
    if tname in ("文件、链接、卡片", "链接", "文件", "小程序", "卡片"):
        return _format_appmsg(content, "链接")
    if tname == "系统消息":
        # 系统消息本身是纯文本提示（如"xxx 加入群聊"），原样返回
        return content
    # 内容本身是 XML 而类型未知：尽力解析，失败回退去标签纯文本
    if content.lstrip().startswith("<"):
        root = _xml_parse(content)
        if root is not None:
            return _format_appmsg(content, tname or "消息")
        plain = _TAG_RE.sub("", content).strip()
        return plain[:200] if plain else f"[{tname or '未知类型'}]"
    return content
