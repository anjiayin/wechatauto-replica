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
# 类型名归一化：微信不同版本把同一类消息写成「文件/链接/卡片」「文件、链接、卡片」
# 「链接」等，去掉分隔符与空白后统一比对（反馈 11：小程序被当成未知类型透传 XML）
_TYPE_SEP_RE = re.compile(r"[\s/／、,，·]+")
# 群消息正文首部的发送者前缀行（"wxid_xxx:" / "群昵称:"，独占首行）
_SENDER_LINE_RE = re.compile(r"^([^\s:：/]{2,64})\s*[:：]\s*$")


def _norm_type(type_name: str) -> str:
    """类型名归一化（去掉 / 、 空格 等分隔符），用于宽松匹配。"""
    return _TYPE_SEP_RE.sub("", str(type_name or ""))


def _strip_sender_prefix(content: str) -> str:
    """去掉正文首部的「发送者ID:\\n」前缀行（微信 4.x 群消息原生格式）。

    仅当前缀独占首行时剥离，避免误吃「9:30 开会」这类正文；用于展示层，
    发送者本身另有权威解析（wechat_service）。
    """
    body = str(content or "").lstrip()
    first, sep, rest = body.partition("\n")
    if sep and _SENDER_LINE_RE.match(first.strip()):
        return rest.lstrip("\n")
    return content


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
    content = _strip_sender_prefix(content)
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
    # 小程序卡片会带来源名称（<sourcedisplayname>），用于替代无意义的跳转链接
    source = _t("sourcedisplayname") or _t("sourceusername")
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
    if kind == "小程序":
        head = f"[小程序] {title}".rstrip()
        if source and source != title:
            head += f"（{source}）"
    else:
        head = f"[{kind}] {title}".rstrip() if title else f"[{kind}]"
    lines = [head]
    if des and des != title:
        lines.append(des[:200])
    # 小程序不给链接（指向的是 mp.weixin 降级页，对用户无意义）
    if url and kind == "链接":
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
    norm = _norm_type(tname)
    if norm == "文本" or not content:
        return content
    # 群消息正文可能带「发送者ID:\n」前缀（反馈 6/11）：展示前剥离
    body = _strip_sender_prefix(content)
    if norm == "图片":
        return "[图片]"
    if norm == "动画表情":
        return "[动画表情]"
    if norm == "语音":
        return "[语音]"
    if norm == "视频":
        return "[视频]"
    if norm == "位置":
        root = _xml_parse(body)
        if root is not None:
            poiname = _find_text(root, "location/poiname") \
                or _find_text(root, "location/label")
            if poiname:
                return f"[位置] {poiname}"
        return "[位置]"
    # 小程序 / 链接 / 文件 / 卡片：类型名各版本写法不同（"/" 或 "、"），
    # 归一化后按关键字匹配，避免漏判后把原始 XML 透传给用户（反馈 11）
    if "小程序" in norm or norm in ("文件链接卡片", "链接", "文件", "卡片"):
        return _format_appmsg(body, "小程序" if "小程序" in norm else "链接")
    if norm == "系统消息":
        # 系统消息本身是纯文本提示（如"xxx 加入群聊"），原样返回
        return content
    # 内容本身是 XML 而类型未知：尽力解析，失败回退去标签纯文本
    if body.lstrip().startswith("<"):
        root = _xml_parse(body)
        if root is not None:
            return _format_appmsg(body, tname or "消息")
        plain = _TAG_RE.sub("", body).strip()
        return plain[:200] if plain else f"[{tname or '未知类型'}]"
    return body if body != content else content
