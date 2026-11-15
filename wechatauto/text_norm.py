# -*- coding: utf-8 -*-
"""微信消息文本的「匹配用归一化」。

为什么需要
----------
微信 4.x 在 @ 提及后会自动插入 U+2005（FOUR-PER-EM SPACE，四分之一 em
空格）。数据库 ``message.content`` 与 UIA 消息行的 ``Name`` 都保留它，例如
本机家人群实测：``'@诸神黄昏\\u20055'``。

而 :meth:`str.split` / ``" ".join(...)``（``app.sender._quote_snippet``
清洗消息片段时用的就是它）会把 U+2005 折叠成普通空格 U+0020。于是用清洗
后的片段 ``'@诸神黄昏 5'`` 去 ``in`` 匹配未经处理的 UIA Name 时，**永远
匹配不上**——这不是随机故障，是确定性失败；随后代码滚动消息列表找目标，
又污染聊天面板状态，表现成「时好时坏」。

因此所有「片段 → 控件 Name / OCR 文本」的比较都必须先经过本模块归一化：
把 Unicode 空格族统一折叠成单个普通空格，再比较。
"""

from __future__ import annotations

import re

# \s 在 Python Unicode 模式下已覆盖绝大多数空白；显式补上常见的
# 不换行空格（U+00A0）、Unicode 空格区间（U+2000~U+200A，含 U+2005）、
# 行/段分隔符、窄不换行空格、全角空格与 BOM，避免不同微信版本/Uia
# provider 输出不一致。
_WS_RE = re.compile(
    r"[\s\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+"
)


def normalize_match_text(value) -> str:
    """把文本压成可用于包含匹配的形式。

    * 所有 Unicode 空白（含 U+2005）→ 单个普通空格；
    * 去掉首尾空白；
    * 保留其余字符（含 ``@``、中文、标点）不做全半角/繁简转换，
      以免引入误匹配。

    返回空串表示「没有可匹配内容」，调用方须据此跳过匹配。
    """
    if value is None:
        return ""
    return _WS_RE.sub(" ", str(value)).strip()


def contains_normalized(needle, haystack) -> bool:
    """归一化后的包含判断；任一为空返回 False（空串不得匹配一切）。"""
    n = normalize_match_text(needle)
    if not n:
        return False
    return n in normalize_match_text(haystack)
