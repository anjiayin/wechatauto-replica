# -*- coding: utf-8 -*-
"""撤回等系统提示消息：XML 转微信客户端同款友好文本。

用户实测消息列表把 ``<sysmsg type="revokemsg">…`` 原样显示成「系统消息」。
本文件锁死 ``content_format.format_content`` 的三条行为：

1. 撤回消息提取 replacemsg/content 里的整句提示（客户端渲染文本），
   去掉全部 XML 标签；
2. 群消息发送者前缀 / XML 声明 / ``<msg>`` 包裹不影响解析；
3. 纯文本系统消息原样返回；未知系统 XML 绝不透传原始标签。
"""
from __future__ import annotations

from app.content_format import format_content

# 用户截图原文（4.x 实测）：只有 <content>，没有 replacemsg
REVOKE_CONTENT = (
    '<sysmsg type="revokemsg"><revokemsg>'
    '<content>"L" 撤回了一条消息</content><revoketime>0</revoketime>'
    '</revokemsg></sysmsg>')

# 部分版本 / 撤回自己消息时客户端渲染用的 replacemsg
REVOKE_REPLACEMSG = (
    '<sysmsg type="revokemsg"><revokemsg>'
    '<session>123@chatroom</session><newmsgid>1</newmsgid>'
    '<replacemsg><![CDATA["张三" 撤回了一条消息]]></replacemsg>'
    '</revokemsg></sysmsg>')


class TestRevokemsg:
    """撤回消息 XML → 纯文本。"""

    def test_content_variant(self):
        assert format_content("系统消息", REVOKE_CONTENT) == '"L" 撤回了一条消息'

    def test_replacemsg_preferred(self):
        assert format_content(
            "系统消息", REVOKE_REPLACEMSG) == '"张三" 撤回了一条消息'

    def test_xml_declaration_prefix(self):
        xml = '<?xml version="1.0" encoding="utf-8"?>' + REVOKE_CONTENT
        assert format_content("系统消息", xml) == '"L" 撤回了一条消息'

    def test_group_sender_prefix_stripped(self):
        """群消息「发送者ID:\\n」前缀不影响 sysmsg 解析。"""
        assert format_content(
            "系统消息", "wxid_sender:\n" + REVOKE_CONTENT
        ) == '"L" 撤回了一条消息'

    def test_msg_wrapper_supported(self):
        xml = f"<msg>{REVOKE_CONTENT}</msg>"
        assert format_content("系统消息", xml) == '"L" 撤回了一条消息'

    def test_no_revoke_word_gets_prefix(self):
        xml = ('<sysmsg type="revokemsg"><revokemsg><content>此消息已被删除'
               '</content></revokemsg></sysmsg>')
        assert format_content("系统消息", xml) == "[撤回消息] 此消息已被删除"

    def test_revokemsg_labeled_as_text_type_still_parsed(self):
        """极端版本把撤回标成「文本」：仍按 sysmsg 转友好文本。"""
        assert format_content("文本", REVOKE_CONTENT) == '"L" 撤回了一条消息'


class TestNonRevokeCompat:
    """其他系统提示 / 纯文本不受影响。"""

    def test_plain_system_text_unchanged(self):
        assert format_content("系统消息", "你已加入群聊") == "你已加入群聊"

    def test_broken_direct_sysmsg_falls_back(self):
        assert format_content("系统消息", '<sysmsg type="revokemsg">') \
            == "[系统消息]"

    def test_unknown_system_xml_not_passthrough(self):
        out = format_content("系统消息", "<foo><bar>提示内容</bar></foo>")
        assert "<" not in out and "提示内容" in out

    def test_other_sysmsg_type_uses_replacemsg(self):
        xml = ('<sysmsg type="pat"><pat><template>拍了拍我</template>'
               '<replacemsg>"李四" 拍了拍我</replacemsg></pat></sysmsg>')
        assert format_content("系统消息", xml) == '"李四" 拍了拍我'

    def test_normal_text_message_unchanged(self):
        assert format_content("文本", "我要一台发动机") == "我要一台发动机"