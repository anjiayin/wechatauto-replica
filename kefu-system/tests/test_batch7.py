# -*- coding: utf-8 -*-
"""批次 7 回归测试（三项需求）。

覆盖：

1. 需求 1 · OE 宽松识别（``app/analyzer.py``）
   - ``loose_oe_hits`` / ``_oe_shape_kind``：**只对局部候选片段**做形状约束
     （段长 ≤4、段数 ≤6、纯数字 9~13 位且最长段 >3、日期形态排除、VIN 17 位
     豁免、短前缀 + 7~9 位件号），**不对全文去分隔符**；
   - 真 OE 全命中：``8K0-615-301`` / ``8K0 615 301`` / ``8K0.615.301`` /
     ``8k0-615-301`` / ``4M0-615-301-A`` / ``4m0-615-301-a`` /
     ``1K0 807 217 C`` / ``313340-32080`` / ``42041-02870`` /
     ``别克V6 26345704 前纵梁``（命中片段 ``V6 26345704``）/
     ``LSG-MJ5P57-8V016154`` / ``LSG MJ5P57 8V016154``；
   - 反例全部不命中：发送人用户名（``zhaoyu_4932403``）、网址
     （``www.cpfly.top`` / ``b23.tv/C4QRGd6``）、日期+时间
     （``2026-11-11 20:56:21``）、``25.1 25.4 26.4 26.8`` /
     ``1-3-7-11-15-22-30``、``u are sexy`` / ``apple music`` /
     ``a100 h100``、``688836.SH``、``zhang.san`` / ``li.si-2024`` /
     ``abc-123-def`` / ``a-b-c-d``；
   - 关键词逻辑不受影响（关键词为空 + 正则不命中 → False）；正则非法仍按
     通过处理（回归）；
   - ``prescreen_hits``：回退命中带「（宽松匹配）」标记（用候选**原文
     片段**，不是紧凑串）并去重。

2. 需求 2 · 图片「不完整」自动重试队列（``app/image_retry.py`` +
   ``app/wechat_service.py``）
   - 队列本体：入队去重、退避排期、``done``/``retry``/``abort`` 三态语义、
     用尽落终态、handler 抛异常不影响后续任务、stop 后可重启、队列满丢
     最旧、非法参数；
   - 服务集成：``MSG_INCOMPLETE`` 入队且不写缓存 → 重试首次仍不完整 →
     第二次成功落库 + 聚合器收到 ``image_sell=True`` + 队列清空；额度不足
     立刻 ``abort`` 不反复烧 token；缓存已有结论直接 ``done`` 且不调模型。

3. 需求 3 · 图片识别开关
   - 开关关闭：视觉调用 0 次、识别队列 0 条、补识别队列 0 条，日志文案
     保持「非文本消息（图片），不分析」；
   - ``/api/status`` 的 ``queue.vision_retry_pending`` 默认 0 与按服务取值。

运行（在 kefu-system 目录下，**必须用全新 basetemp 目录**）::

    python -m pytest tests/test_batch7.py -q --basetemp=D:/tmp/kefu-pytest-b7a
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import analyzer, vision
from app.api import ALL_ROUTERS, deps
from app.config import KEY_IMAGE_VISION_ENABLED
from app.db import Database
from app.image_retry import ImageRetryQueue
from app.notifier import Notifier
from app.wechat_service import WeChatService
from tests.test_api import FakePipeline, FakeWechatService
from tests.test_batch6_media import (
    _complete_jpeg,
    image_msg,
    make_svc,
    skipped_logs,
    vehicle_result,
    vision_workers,
    wait_processed,
)

USER = "21264829018@chatroom"

# 与前端 ``frontend/src/constants/prescreen.ts::PRESCREEN_PRESET_OE`` 逐字一致
OE_PRESET = (
    r"(?<![A-Za-z0-9])(?:\d{4,5}-[A-Z0-9]{3,7}(?:-[A-Z0-9]{2,4})?"
    r"|[A-Z0-9]{4}-\d{7}-[A-Za-z]"
    r"|[A-Z]{1,3}[- ]?\d{2,3}[- ]?\d{3}[- ]?\d{1,3}(?:[- ]?\d{1,3})?"
    r"|\d{10,13}"
    r"|(?i:(?=[A-Z0-9]*[A-Z])[A-Z0-9]{8,15}))(?![A-Za-z0-9])"
)


def wait_until(pred, timeout: float = 5.0) -> bool:
    """轮询等待条件成立（避免固定 sleep 造成的偶发失败）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


# ===========================================================================
# 需求 1：OE 宽松识别（候选片段 + 形状约束，**不对全文紧凑化**）
# ===========================================================================
# 真 OE（都不能丢）：各种分隔符写法 + 小写 + 拆开的 VIN + 通用 8 位件号
POSITIVE_OE = [
    "8K0-615-301",
    "8K0 615 301",
    "8K0.615.301",
    "8k0-615-301",
    "4M0-615-301-A",
    "4m0-615-301-a",
    "1K0 807 217 C",
    "313340-32080",
    "42041-02870",
    "LSG-MJ5P57-8V016154",
    "LSG MJ5P57 8V016154",
]

# 「别克V6 26345704 前纵梁」这种通用 8 位件号（D 规则）：原预设只认 10~13
# 位数字，这条真商机本来是漏的，必须由回退保住
POSITIVE_OE_D_RULE = "别克V6 26345704 前纵梁  左    谁🈶？？？？"

# 反例（必须保持不命中）：发送人用户名 / 网址 / 日期+时间 / 英文连读 /
# 股票代码与短链 / 小数串与递增序列 / 各种「像但不像 OE」
NEGATIVE_OE = [
    "[13:42] zhaoyu_4932403:\n创业板60F中上了55天线了",
    "[11:48] Maiale:\nhttps://www.cpfly.top/",
    "[20:56] UIA 性能修复实测 2026-11-11 20:56:21",
    "2026-11-11 20:56:21",
    "2026-11-11",
    "过去只有25.1 25.4  26.4 26.8",
    "前7天，我用的螺旋递增补单。 1-3-7-11-15-22-30，没有效果",
    "u are sexy",
    "我的apple music",
    "宇树科技（688836.SH）中签500股",
    "https://b23.tv/C4QRGd6",
    "国外数据中心淘汰的a100 h100很多",
    "zhang.san",
    "li.si-2024",
    "abc-123-def",
    "a-b-c-d",
]


class TestOeShapeKind:
    @pytest.mark.parametrize("cand,kind", [
        ("313340-32080", "num"),
        ("42041-02870", "num"),
        ("V6 26345704", "num"),          # D：短混合前缀 + 8 位件号
        ("8K0-615-301", "seg"),
        ("4M0-615-301-A", "seg"),
        ("A000 420 26 20", "seg"),
        ("1K0 807 217 C", "seg"),
        ("8k0-615-301", "seg"),          # 全小写：靠 B2 段约束命中
        ("LSG-MJ5P57-8V016154", "vin"),
    ])
    def test_accepted_kinds(self, cand, kind):
        assert analyzer._oe_shape_kind(cand) == kind

    @pytest.mark.parametrize("cand", [
        "zhaoyu_4932403",       # 下划线不是分隔符 → 单段过长
        "www.cpfly.top",        # cpfly = 5 位 > 段长上限 4
        "2026-11-11",           # 8 位数字 < 数字下限 9
        "2026-11-11 20:56",     # 日期形态
        "1-3-7-11-15-22-30",    # 每段 ≤3 位（递增序列）
        "25.1 25.4 26.4",       # 每段 ≤3 位（小数串）
        "zhang.san",            # 无数字
        "li.si-2024",           # 无字母数字混合段
        "abc-123-def",          # 无字母数字混合段
        "a-b-c-d",              # 紧凑后 4 位，长度不足
        "688836.SH",            # 段长 6 > 4
        "b23.tv/C4QRGd6",       # 段长 7 > 4
        "u are sexy",           # 无数字
        "a100 h100",            # 段数不足且无混合+纯数字段组合
        "",
    ])
    def test_rejected(self, cand):
        assert analyzer._oe_shape_kind(cand) == ""


class TestLooseOeHits:
    @pytest.mark.parametrize("text", POSITIVE_OE)
    def test_positive_hits(self, text):
        assert analyzer.loose_oe_hits(text) == [text]

    def test_d_rule_keeps_8_digit_part_number(self):
        """``别克V6 26345704`` 的命中片段是 ``V6 26345704``（真商机不能丢）。"""
        assert analyzer.loose_oe_hits(POSITIVE_OE_D_RULE) == ["V6 26345704"]

    @pytest.mark.parametrize("text", [
        "1-3-7-11-15-22-30",
        "前7天，我用的螺旋递增补单。 1-3-7-11-15-22-30，没有效果",
        "25.1 25.4 26.4 26.8",
        "过去只有25.1 25.4  26.4 26.8",
    ])
    def test_numeric_sequence_rejected(self, text):
        """A 类里「每段都 ≤3 位」= 数字序列/小数串，不是 OE（_OE_SHORT_SEG=3）。"""
        assert analyzer.loose_oe_hits(text) == []
        assert analyzer.prescreen(text, "", OE_PRESET) is False

    @pytest.mark.parametrize("text,expected", [
        ("V6 26345704", ["V6 26345704"]),
        ("8K0 26345704", ["8K0 26345704"]),
        (POSITIVE_OE_D_RULE, ["V6 26345704"]),
    ])
    def test_short_prefix_long_part_number(self, text, expected):
        """D 规则：短混合前缀 + 7~9 位纯数字段（通用/别克 8 位件号）。"""
        assert analyzer.loose_oe_hits(text) == expected
        assert analyzer._oe_shape_kind(expected[0]) == "num"
        assert analyzer.prescreen(text, "", OE_PRESET) is True

    @pytest.mark.parametrize("text", [
        "a100 h100",          # 两段都是字母数字混合、且无 7~9 位纯数字段
        "688836.SH",          # 前缀不是短混合段
        "apple music",        # 无数字
    ])
    def test_short_prefix_rule_does_not_over_reach(self, text):
        """D 规则不能放大召回：这些「像但不像件号」的必须仍不命中。"""
        assert analyzer.loose_oe_hits(text) == []

    def test_adjacent_part_numbers_split(self):
        """空格把相邻两个件号粘成一个超长候选时，按**段边界**切开逐个重判。

        拆车件群典型写法：``8K0-615-301 4M0-615-301 有吗`` —— 整段候选紧凑后
        18 位 > 15 会被直接拒绝，两个真 OE 一起丢。必须拆开都识别出来。
        """
        assert analyzer.loose_oe_hits("8K0-615-301 4M0-615-301 有吗") == \
            ["8K0-615-301", "4M0-615-301"]
        assert analyzer.loose_oe_hits("8K0-615-301 8K0-615-302 各要一个") == \
            ["8K0-615-301", "8K0-615-302"]
        # 完全重复的相邻件号只记一条
        assert analyzer.loose_oe_hits("8K0-615-301 8K0-615-301") == \
            ["8K0-615-301"]
        # 后面跟数量/单位也要能取到件号
        assert analyzer.loose_oe_hits("313340-32080 x 2") == ["313340-32080"]
        assert analyzer.prescreen("8K0-615-301 4M0-615-301 有吗",
                                  "", OE_PRESET) is True

    def test_space_joined_single_oe_not_split(self):
        """单个带空格的件号（``8K0 615 301``）整段已合格，不能被切成三段。"""
        assert analyzer.loose_oe_hits("8K0 615 301 到货") == ["8K0 615 301"]
        assert analyzer.loose_oe_hits("1K0 807 217 C 有吗") == ["1K0 807 217 C"]

    # --- 批次 7 QA 复验 C-1 / C-2 / C-3：混用分隔符 / 斜杠切分  ---
    def test_mixed_separator_fragment_not_reported(self):
        """QA-C-2：返回的每一条都必须是**完整件号**，不能有半截畸形片段。

        旧实现候选正则写死最多 7 段，断在 ``8K0`` 上，下一个匹配从 ``615-003``
        起，于是产出 ``615-003 8K0-615-004``——丢前缀、两个件号粘成一条，却
        通过了形状判定被当成「合格件号」写进商机要点（展示错件号比不展示更
        误导）。去掉段数上限后整条消息是一个完整候选，按空白切分不会切在
        件号中间，4 个件号一个都不漏。
        """
        hits = analyzer.loose_oe_hits(
            "8K0-615-001 8K0-615-002 8K0-615-003 8K0-615-004")
        assert hits == ["8K0-615-001", "8K0-615-002",
                        "8K0-615-003", "8K0-615-004"]
        for h in hits:
            assert " " not in h                          # 不能是多件号粘连串
            assert h.startswith("8K0-615-")              # 不能丢前缀
            assert h.count("8K0") == 1
            assert analyzer._oe_shape_kind(h)            # 每条都能独立判为合格
            assert analyzer._oe_seps_homogeneous(h)      # 分隔符同类
        assert "615-003 8K0-615-004" not in hits
        assert analyzer.loose_oe_hits(
            "8K0-615-301 8K0-615-302 8K0-615-303 各一个") == \
            ["8K0-615-301", "8K0-615-302", "8K0-615-303"]

    def test_slash_separated_oe_pair(self):
        """QA-C-3：``/`` 是件号之间的天然分界，必须在切分集合里。"""
        assert analyzer.loose_oe_hits("8K0-615-301/4M0-615-301") == \
            ["8K0-615-301", "4M0-615-301"]
        assert analyzer.loose_oe_hits("8K0-615-301/4M0-615-301 各一") == \
            ["8K0-615-301", "4M0-615-301"]
        assert analyzer.prescreen("8K0-615-301/4M0-615-301",
                                  "", OE_PRESET) is True
        # 英文/全角逗号天然断词（不在分隔符集合里），无需额外处理
        assert analyzer.loose_oe_hits("8K0-615-301,4M0-615-301") == \
            ["8K0-615-301", "4M0-615-301"]
        assert analyzer.loose_oe_hits("8K0-615-301，4M0-615-301") == \
            ["8K0-615-301", "4M0-615-301"]

    def test_homogeneous_separator_required(self):
        """混用分隔符的串几乎必然是「两个件号被粘在一起」，不得整体接受。

        ``8K0-615-301 4M0`` 能过形状判定（compact 13 位 / 段长 4 / 段数 4 /
        含大写），但 ``-`` 和空格混用 → 不可信，只能切出干净的那个件号。
        """
        assert analyzer._oe_seps_homogeneous("8K0-615-301 4M0") is False
        assert analyzer._oe_seps_homogeneous("8K0-615-301") is True
        assert analyzer._oe_seps_homogeneous("A000 420 26 20") is True
        hits = analyzer.loose_oe_hits("8K0-615-301 4M0")
        assert "8K0-615-301 4M0" not in hits
        assert hits == ["8K0-615-301"]

    def test_two_space_written_oe_known_limitation(self):
        """**已知限制，刻意不修**：两个都用空格写的件号紧邻时识别不出。

        ``8K0 615 301 8K0 615 302`` 返回 ``[]``——空格既是件号内部的分隔符
        （``8K0 615 301``、``1K0 807 217 C`` 都是这种写法），又是件号之间的
        天然分界；没有更多上下文时「两个 3 段件号」与「一个 6 段件号」在文本
        上无法区分。按空白切完只剩 ``8K0``/``615``/``301``，单独看全不合格。

        这是**刻意的取舍：宁可漏这一种少见写法，也不吐出错误件号**。未来若要
        支持，需引入分词器/件号词典级别的上下文，不能靠正则堆规则。

        代价说清楚：这一形态不仅**不展示**，预筛层也不会命中（``prescreen``
        返回 ``False``，整条消息不会送 AI）——**确实会丢掉这一种写法的商机**，
        是明知代价后接受的（展示错误件号 / 拒错件号给客户，比漏一条更糟）。
        """
        assert analyzer.loose_oe_hits("8K0 615 301 8K0 615 302") == []
        assert analyzer.loose_oe_hits("8K0 615 001 8K0 615 002") == []
        assert analyzer.prescreen("8K0 615 301 8K0 615 302",
                                  "", OE_PRESET) is False

    def test_pathological_input_stays_empty(self):
        """病态超长输入不炸也不误命中。

        ``"1-" * 20000`` 若 C 类 VIN 只按「17 位 + 字符集」判定，17 个 ``1``
        会被当成 VIN 误命中——VIN 必须**同时含字母和数字**（本轮修复项）。
        """
        assert analyzer.loose_oe_hits("1-" * 20000) == []
        assert analyzer.loose_oe_hits("a " * 20000) == []
        assert analyzer.loose_oe_hits("A " * 20000) == []
        assert analyzer.loose_oe_hits("12345678 " * 20000) == []
        assert analyzer._oe_shape_kind("1-" * 16 + "1") == ""
        assert analyzer._oe_shape_kind("LSG-MJ5P57-8V016154") == "vin"

    @pytest.mark.parametrize("text", [
        "8K0\uff0d615\uff0d301",      # U+FF0D 全角减号（中文输入法最常见）
        "8K0\u2013615\u2013301",      # U+2013 en dash
        "8K0\u2010615\u2010301",      # U+2010 hyphen
        "8K0\u2011615\u2011301",      # U+2011 不换行连字符
        "8K0\ufe63615\ufe63301",      # U+FE63 小写全角减号
        "谁有 8K0\uff0d615\uff0d301 报价",  # 句中出现
    ])
    def test_fullwidth_hyphen_variants_hit(self, text):
        """全角减号 / 各种连字符变体（中文输入法、Word 转贴）必须认得出。

        批次 7 QA-P2-1：``8K0－615－301`` 原本完全漏识别，属真漏商机。
        """
        assert analyzer.loose_oe_hits(text)
        assert analyzer.prescreen(text, "", OE_PRESET) is True

    @pytest.mark.parametrize("text", [
        "8K0\u2014615\u2014301",      # U+2014 em dash = 中文破折号，刻意不认
        "8K0\uff5e615\uff5e301",      # U+FF5E 全角波浪线
        "8K0~615~301",               # ASCII 波浪线
        "8000~12000元",               # 价格区间：粘起来会变成 800012000
        "1200\uff5e1500元",
        "2026\uff0d11\uff0d11",       # 全角减号日期：必须仍被日期形态挡住
        "2026\u201311\u201311",
    ])
    def test_fullwidth_hyphen_does_not_over_reach(self, text):
        """破折号/波浪线/价格区间/全角日期一律不命中（不放大召回）。"""
        assert analyzer.loose_oe_hits(text) == []
        assert analyzer.prescreen("8K0 615 301 到货", "", OE_PRESET) is True

    @pytest.mark.parametrize("text", NEGATIVE_OE)
    def test_negative_no_hit(self, text):
        assert analyzer.loose_oe_hits(text) == []

    def test_bare_compact_form_not_needed(self):
        """连写的 ``8K0615301`` 本来就被原正则第 5 类覆盖，不需要回退。"""
        assert analyzer.loose_oe_hits("8K0615301") == []

    def test_underscore_is_not_separator(self):
        """下划线是用户名特征，绝不能被当成 OE 分隔符。"""
        assert analyzer.loose_oe_hits("NoO_x1ao9 说 8K0-615-301") == \
            ["8K0-615-301"]

    def test_chinese_not_glued(self):
        """中文句子里的中文标点/中文之间空格不参与分隔符（不粘出假长数字）。"""
        text = "2020年11月12日 10点30分 到货"
        assert analyzer.loose_oe_hits(text) == []

    def test_dedupe_and_order(self):
        text = "先看 313340-32080 再看 8K0-615-301 再看 313340-32080"
        assert analyzer.loose_oe_hits(text) == ["313340-32080", "8K0-615-301"]

    def test_capped_at_ten(self):
        text = " ".join(["8K0-615-%03d" % i for i in range(1, 20)])
        assert len(analyzer.loose_oe_hits(text)) <= 10

    def test_empty_input(self):
        assert analyzer.loose_oe_hits("") == []
        assert analyzer.loose_oe_hits(None) == []


class TestPrescreenLooseOe:
    @pytest.mark.parametrize("text", POSITIVE_OE)
    def test_loose_oe_hits(self, text):
        """分隔符不标准 / 小写 / 拆开的 VIN 都能过预筛（不能丢商机）。"""
        assert analyzer.prescreen(f"这个件有吗 {text} 报价", "", OE_PRESET) is True

    @pytest.mark.parametrize("text", NEGATIVE_OE)
    def test_negative_stays_false(self, text):
        """用户名 / 网址 / 日期时间 等一律不因回退而误通过。"""
        assert analyzer.prescreen(text, "", OE_PRESET) is False

    def test_d_rule_message_passes(self):
        """通用 8 位件号（别克V6 26345704）原本漏检，回退必须能救回来。"""
        assert analyzer.prescreen(POSITIVE_OE_D_RULE, "", OE_PRESET) is True

    def test_keyword_logic_unaffected(self):
        """关键词仍只在原文匹配；关键词为空且正则不命中 → False。"""
        assert analyzer.prescreen("这个前杠多少钱", "", OE_PRESET) is False
        assert analyzer.prescreen("这个前杠多少钱", "前杠", "") is True
        assert analyzer.prescreen("这个前杠多少钱", "后杠", "") is False
        # 空关键词 + 空正则 → 全部通过（预筛不误伤）
        assert analyzer.prescreen("随便一句", "", "") is True

    def test_original_hit_still_works(self):
        """原样就能命中的 OE，行为不变（回退不影响结论）。"""
        assert analyzer.prescreen("42431-0K090 现货", "", OE_PRESET) is True

    def test_invalid_regex_passes_as_before(self):
        """正则非法：打 warning 并按通过处理（回归，与批次 6 口径一致）。"""
        assert analyzer.prescreen("随便一句", "", "([unclosed") is True


class TestPrescreenHits:
    def test_original_hits_format_unchanged(self):
        hits = analyzer.prescreen_hits("42431-0K090 现货", "", OE_PRESET)
        assert hits == ["42431-0K090"]

    def test_loose_hit_is_marked_with_candidate_text(self):
        """回退命中写「候选原文片段（宽松匹配）」，不是紧凑串。"""
        hits = analyzer.prescreen_hits("8K0-615-301 有吗", "", OE_PRESET)
        assert hits == ["8K0-615-301（宽松匹配）"]

    def test_loose_hit_deduped(self):
        """同一候选只记一条；原文已命中时不重复记。"""
        hits = analyzer.prescreen_hits("8K0-615-301 和 8K0-615-301",
                                       "", OE_PRESET)
        assert hits == ["8K0-615-301（宽松匹配）"]
        # 原文命中「8K0615301」后，同一 OE 的带横杠写法仍单独标记（可读性好）
        hits2 = analyzer.prescreen_hits("8K0615301", "", OE_PRESET)
        assert hits2 == ["8K0615301"]

    def test_keyword_and_loose_hits_coexist(self):
        hits = analyzer.prescreen_hits("前杠 8K0-615-301", "前杠", OE_PRESET)
        assert hits[0] == "前杠"
        assert "8K0-615-301（宽松匹配）" in hits

    def test_invalid_regex_does_not_raise(self):
        """非法正则：只打 warning，不抛异常（关键词/宽松 OE 仍能给出原因）。"""
        assert analyzer.prescreen_hits("随便一句", "", "([unclosed") == []
        # 正则非法时 prescreen 已经「按通过处理」，这里多记一条宽松命中也无害
        assert analyzer.prescreen_hits("前杠 8K0-615-301", "前杠",
                                       "([unclosed") == [
            "前杠", "8K0-615-301（宽松匹配）"]

    def test_hits_capped_at_ten(self):
        text = " ".join(["8K0-615-%03d" % i for i in range(1, 20)])
        assert len(analyzer.prescreen_hits(text, "", OE_PRESET)) <= 10

    def test_no_malformed_fragment_in_hits(self):
        """QA-C-2：写进商机要点的必须是完整件号，不能是半截粘连片段。

        旧实现会产出 ``615-003 8K0-615-004（宽松匹配）`` 这种「丢前缀 + 两个
        件号粘一起」的畸形条目——展示错误件号比不展示更误导用户。
        """
        hits = analyzer.prescreen_hits(
            "8K0-615-001 8K0-615-002 8K0-615-003 8K0-615-004",
            "", OE_PRESET)
        assert hits == ["8K0-615-00%d（宽松匹配）" % i for i in range(1, 5)]
        for h in hits:
            body = h.replace("（宽松匹配）", "")
            assert body.startswith("8K0-615-")
            assert body.count("8K0") == 1


# ===========================================================================
# 需求 2：图片补识别队列（队列本体）
# ===========================================================================
def _task(local_id: int = 2467, username: str = USER) -> dict:
    return {"username": username, "local_id": local_id, "mtype": "图片",
            "kind": "group", "session_name": "外协群",
            "sender_wxid": "wxid_other", "sender_name": "李四",
            "create_time": time.time(), "content": "<msg/>"}


class TestImageRetryQueue:
    def test_enqueue_dedupes_same_local_id(self):
        q = ImageRetryQueue(lambda t: "done")
        assert q.enqueue(USER, 1, _task(1)) is True
        assert q.enqueue(USER, 1, _task(1)) is False      # 在途任务去重
        assert q.pending_count() == 1
        assert q.status()["enqueued"] == 1

    @pytest.mark.parametrize("username,local_id", [
        ("", 1), (USER, 0), (USER, -3), (USER, "abc"), (None, 1),
    ])
    def test_enqueue_rejects_illegal_params(self, username, local_id):
        q = ImageRetryQueue(lambda t: "done")
        assert q.enqueue(username, local_id, _task()) is False
        assert q.pending_count() == 0

    def test_done_semantics(self):
        calls: list = []
        q = ImageRetryQueue(lambda t: calls.append(dict(t)) or "done",
                            delays=(0.02, 0.02))
        q.enqueue(USER, 1, _task(1))
        q.start()
        try:
            assert wait_until(lambda: q.status()["succeeded"] == 1)
        finally:
            q.stop()
        assert q.pending_count() == 0
        # handler 能拿到 retry_attempt（从 1 开始），便于日志写「第 N 次重试」
        assert calls[0]["retry_attempt"] == 1
        assert calls[0]["local_id"] == 1

    def test_retry_then_done_schedules_backoff(self):
        calls: list = []

        def handler(task):
            calls.append(task["retry_attempt"])
            return "retry" if len(calls) == 1 else "done"

        q = ImageRetryQueue(handler, delays=(0.05, 0.05, 0.05))
        q.enqueue(USER, 1, _task(1))
        q.start()
        try:
            assert wait_until(lambda: q.status()["succeeded"] == 1)
        finally:
            q.stop()
        st = q.status()
        assert calls == [1, 2]           # 第 1 次 retry → 第 2 次 done
        assert st["retried"] == 1
        assert st["pending"] == 0

    def test_abort_stops_immediately(self):
        calls: list = []
        q = ImageRetryQueue(lambda t: calls.append(t) or "abort",
                            delays=(0.02, 0.02, 0.02))
        q.enqueue(USER, 1, _task(1))
        q.start()
        try:
            assert wait_until(lambda: q.status()["abandoned"] == 1)
            time.sleep(0.1)              # 给足时间确认不会再有第二次调用
        finally:
            q.stop()
        assert len(calls) == 1
        assert q.pending_count() == 0

    def test_exhausted_lands_terminal_state(self):
        calls: list = []
        q = ImageRetryQueue(lambda t: calls.append(t) or "retry",
                            delays=(0.02, 0.02, 0.02))
        q.enqueue(USER, 1, _task(1))
        q.start()
        try:
            assert wait_until(lambda: q.status()["abandoned"] == 1)
        finally:
            q.stop()
        assert len(calls) == 3           # delays 长度 = 最大尝试次数
        assert q.status()["attempts"] == 3
        assert q.pending_count() == 0

    def test_handler_exception_does_not_kill_worker(self):
        seen: list = []

        def handler(task):
            seen.append(task["local_id"])
            if task["local_id"] == 1:
                raise RuntimeError("handler 炸了")
            return "done"

        q = ImageRetryQueue(handler, delays=(0.02,))
        q.enqueue(USER, 1, _task(1))
        q.enqueue(USER, 2, _task(2))
        q.start()
        try:
            assert wait_until(lambda: q.status()["succeeded"] == 1)
        finally:
            q.stop()
        assert seen == [1, 2]            # 第 1 条异常后第 2 条仍被处理
        assert q.pending_count() == 0

    def test_stop_then_start_again(self):
        q = ImageRetryQueue(lambda t: "done", delays=(0.1,))
        q.start()
        q.stop()
        q.enqueue(USER, 1, _task(1))
        q.start()
        try:
            assert wait_until(lambda: q.status()["succeeded"] == 1)
        finally:
            q.stop()

    def test_queue_full_drops_oldest(self):
        q = ImageRetryQueue(lambda t: "retry", delays=(0.05,),
                            max_pending=2)
        assert q.enqueue(USER, 1, _task(1)) is True
        assert q.enqueue(USER, 2, _task(2)) is True
        assert q.enqueue(USER, 3, _task(3)) is True     # 丢最旧的 1
        st = q.status()
        assert st["dropped"] == 1
        assert q.pending_count() == 2
        assert sorted(j.local_id for j in q._jobs) == [2, 3]

    def test_status_fields(self):
        q = ImageRetryQueue(lambda t: "done", delays=(10.0, 30.0))
        q.enqueue(USER, 1, _task(1))
        st = q.status()
        for key in ("pending", "next_in_sec", "attempts", "enqueued",
                    "succeeded", "retried", "abandoned", "dropped", "delays"):
            assert key in st
        assert st["delays"] == [10.0, 30.0]
        assert st["next_in_sec"] >= 1    # 首次尝试要等 delays[0]

    def test_on_drop_callback_reports_dropped_job(self):
        """满队列丢最旧时回调 on_drop，带上被丢的 username/local_id。"""
        seen: list = []
        q = ImageRetryQueue(lambda t: "done", max_pending=1,
                            on_drop=lambda u, l, t: seen.append((u, l)))
        assert q.enqueue(USER, 1, _task(1)) is True
        assert q.enqueue(USER, 2, _task(2)) is True
        assert seen == [(USER, 1)]
        assert q.pending_count() == 1
        assert q.status()["dropped"] == 1

    def test_on_drop_callback_exception_swallowed(self):
        """on_drop 抛异常不能影响入队（补识别是附加能力）。"""

        def _boom(username, local_id, task):
            raise RuntimeError("回调炸了")

        q = ImageRetryQueue(lambda t: "done", max_pending=1, on_drop=_boom)
        assert q.enqueue(USER, 1, _task(1)) is True
        assert q.enqueue(USER, 2, _task(2)) is True
        assert q.pending_count() == 1

    def test_on_drop_not_called_for_illegal_params(self):
        """非法参数被拒不是「被挤掉」，不该回调（否则用户多一条莫名日志）。"""
        seen: list = []
        q = ImageRetryQueue(lambda t: "done", max_pending=1,
                            on_drop=lambda u, l, t: seen.append((u, l)))
        assert q.enqueue("", 1, _task(1)) is False
        assert q.enqueue(USER, 0, _task(1)) is False
        assert q.enqueue(USER, "abc", _task(1)) is False
        assert seen == []

    def test_pending_does_not_include_running_job(self):
        """pending 不含正在执行的那一条（QA-P3-4 语义，避免误判「已完成」）。"""
        started = threading.Event()
        release = threading.Event()

        def handler(task):
            started.set()
            release.wait(2)
            return "done"

        q = ImageRetryQueue(handler, delays=(0.1,))
        q.enqueue(USER, 1, _task(1))
        q.start()
        try:
            assert started.wait(2)
            assert q.pending_count() == 0          # 任务在飞，但队列里没有
            assert q.status()["pending"] == 0
        finally:
            release.set()
            assert wait_until(lambda: q.status()["succeeded"] == 1)
            q.stop()


# ===========================================================================
# 需求 2：服务集成（wechat_service）
# ===========================================================================
def _fast_retry(svc: WeChatService, delays=(0.02, 0.02, 0.02)):
    """把服务里的补识别队列换成极短退避版本（测试不依赖真实 10s 等待）。"""
    svc._image_retry = ImageRetryQueue(
        svc._retry_image_task, delays=delays,
        on_drop=svc._on_image_retry_dropped)
    return svc._image_retry


class TestImageRetryService:
    def test_incomplete_enqueues_without_cache(self, monkeypatch):
        """MSG_INCOMPLETE → 入队 1 条，不写缓存，日志带 local_id。"""
        svc, db = make_svc()

        def _raise(data, **kw):
            raise vision.VisionError(vision.MSG_INCOMPLETE)

        monkeypatch.setattr(svc, "get_image_bytes",
                            lambda u, l: _complete_jpeg())
        monkeypatch.setattr(vision, "analyze_image", _raise)
        with vision_workers(svc):
            svc._on_message(image_msg())
            assert wait_processed(svc)
        assert svc.image_retry_pending == 1
        assert db.get_image_analysis(USER, 2467) is None
        error = skipped_logs(db)[0]["error"]
        assert "不完整" in error
        assert "已排入重试队列" in error
        assert "local_id=2467" in error

    def test_image_read_failure_enqueues_retry(self, monkeypatch):
        """取不到图（原图未下载完的主路径）同样要排队，不能永久漏识别。"""
        svc, db = make_svc()
        monkeypatch.setattr(svc, "get_image_bytes", lambda u, l: None)
        with vision_workers(svc):
            svc._on_message(image_msg())
            assert wait_processed(svc)
        assert svc.image_retry_pending == 1
        error = skipped_logs(db)[0]["error"]
        assert error.startswith("图片读取失败或不完整，未识别")
        assert "已排入重试队列" in error

    def test_retry_eventually_succeeds(self, monkeypatch):
        """首次仍不完整 → "retry"；第二次取到图 → 落库 + 聚合 + 队列清空。"""
        units: list = []
        svc, db = make_svc(units)
        calls = {"n": 0}

        def _flaky(data, **kw):
            calls["n"] += 1
            # 第 1 次（worker）与第 2 次（重试）都不完整，第 3 次成功
            if calls["n"] <= 2:
                raise vision.VisionError(vision.MSG_INCOMPLETE)
            return vehicle_result(part="发动机总成", vin="LSGMJ5P578V016154")

        monkeypatch.setattr(svc, "get_image_bytes",
                            lambda u, l: _complete_jpeg())
        monkeypatch.setattr(vision, "analyze_image", _flaky)
        svc._image_retry = _fast_retry(svc)
        retry = svc._image_retry
        retry.start()
        try:
            with vision_workers(svc):
                svc._on_message(image_msg())
                assert wait_processed(svc)
                # 等补识别队列真正得出结论（不能只看 pending：任务在被处理
                # 的瞬间也会短暂不在队列里）
                assert wait_until(lambda: retry.status()["succeeded"] == 1)
                assert wait_until(lambda: svc.image_retry_pending == 0)
        finally:
            retry.stop()
        assert calls["n"] == 3
        row = db.get_image_analysis(USER, 2467)
        assert row is not None and row["is_vehicle_part"] == 1
        svc._aggregator.stop(flush_all=True)
        assert len(units) == 1
        assert units[0].has_image_sell is True
        # 每次尝试都留痕，且带「第 N 次重试」字样与 local_id
        errors = [log["error"] for log in skipped_logs(db)]
        assert any("第 1 次重试" in e and "local_id=2467" in e for e in errors)
        assert any("补识别成功" in e for e in errors)

    def test_quota_error_aborts_without_burning_tokens(self, monkeypatch):
        """额度不足 → "abort"：入队后只多调一次，不再继续重试。"""
        svc, db = make_svc()
        calls = {"n": 0}

        def _flaky(data, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise vision.VisionError(vision.MSG_INCOMPLETE)
            raise vision.VisionError("图片识别失败：额度不足或触发限流")

        monkeypatch.setattr(svc, "get_image_bytes",
                            lambda u, l: _complete_jpeg())
        monkeypatch.setattr(vision, "analyze_image", _flaky)
        retry = _fast_retry(svc)
        retry.start()
        try:
            with vision_workers(svc):
                svc._on_message(image_msg())
                assert wait_processed(svc)
                assert wait_until(lambda: retry.status()["abandoned"] == 1)
                time.sleep(0.1)
        finally:
            retry.stop()
        assert calls["n"] == 2            # worker 1 次 + 重试 1 次，之后放弃
        assert retry.status()["attempts"] == 1
        assert svc.image_retry_pending == 0
        assert db.get_image_analysis(USER, 2467) is None
        errors = [log["error"] for log in skipped_logs(db)]
        assert any("额度不足" in e for e in errors)

    def test_cached_conclusion_returns_done_without_vision(self, monkeypatch):
        """缓存已有结论 → "done"，且绝不调用视觉模型（不重复烧 token）。"""
        svc, db = make_svc()
        db.save_image_analysis(USER, 2467, is_vehicle_part=True,
                               part_name="轮毂", vin="")
        called = {"n": 0}

        def _never(data, **kw):
            called["n"] += 1
            return vehicle_result()

        monkeypatch.setattr(svc, "get_image_bytes", lambda u, l: None)
        monkeypatch.setattr(vision, "analyze_image", _never)
        assert svc._retry_image_task(_task(2467)) == "done"
        assert called["n"] == 0
        assert svc._aggregator.pending_count() == 1     # 结论复用，仍进聚合

    def test_read_failure_returns_retry(self, monkeypatch):
        """取不到图 → "retry"（等微信下载完原图）。"""
        svc, db = make_svc()
        monkeypatch.setattr(svc, "get_image_bytes", lambda u, l: None)
        assert svc._retry_image_task(_task(2467)) == "retry"
        assert "第 1 次重试" in skipped_logs(db)[0]["error"]

    def test_switch_off_during_retry_aborts(self, monkeypatch):
        """排队期间用户关掉开关 → 立刻放弃，绝不再调视觉模型（零 token）。"""
        svc, db = make_svc()
        db.set_setting(KEY_IMAGE_VISION_ENABLED, "0")
        called = {"n": 0}

        def _never(data, **kw):
            called["n"] += 1
            return vehicle_result()

        monkeypatch.setattr(svc, "get_image_bytes",
                            lambda u, l: _complete_jpeg())
        monkeypatch.setattr(vision, "analyze_image", _never)
        assert svc._retry_image_task(_task(2467)) == "abort"
        assert called["n"] == 0
        assert "开关已关闭" in skipped_logs(db)[0]["error"]

    def test_unexpected_exception_returns_abort(self, monkeypatch):
        """非 VisionError 的异常 → "abort"（不把异常抛回调度线程）。"""
        svc, db = make_svc()
        monkeypatch.setattr(svc, "get_image_bytes",
                            lambda u, l: _complete_jpeg())

        def _boom(data, **kw):
            raise RuntimeError("网关返回了意料之外的东西")

        monkeypatch.setattr(vision, "analyze_image", _boom)
        assert svc._retry_image_task(_task(2467)) == "abort"
        assert "已放弃重试" in skipped_logs(db)[0]["error"]

    def test_service_start_stop_lifecycle_covers_retry_queue(self, monkeypatch,
                                                             tmp_path):
        """start()/stop() 生命周期里补识别队列线程也跟着起停。"""
        svc, _db = make_svc()
        monkeypatch.setattr(svc, "configured_cache_dir", lambda: tmp_path)
        svc.start()
        try:
            thread = svc._image_retry._thread
            assert thread is not None and thread.is_alive()
            assert thread.name == "kefu-image-retry"
        finally:
            svc.stop()
        assert svc._image_retry._thread is None

    def test_queue_full_drop_writes_visible_log(self):
        """QA-P3-2：队列满丢最旧任务时，必须在分析日志里留一条可见记录。"""
        svc, db = make_svc()
        svc._image_retry = ImageRetryQueue(
            svc._retry_image_task, max_pending=1,
            on_drop=svc._on_image_retry_dropped)
        assert svc._image_retry.enqueue(USER, 2467, _task(2467)) is True
        assert svc._image_retry.enqueue(USER, 2468, _task(2468)) is True
        assert svc._image_retry.pending_count() == 1
        assert svc._image_retry.status()["dropped"] == 1
        logs = skipped_logs(db)
        assert len(logs) == 1
        error = logs[0]["error"]
        assert error.startswith("图片补识别队列已满，已丢弃最旧任务")
        assert "local_id=2467" in error          # 被丢的是最旧那条
        assert "2468" not in error


# ===========================================================================
# 需求 3：图片识别开关（关闭后零 token）+ /api/status 新字段
# ===========================================================================
class TestImageSwitchOff:
    def test_switch_off_no_vision_no_queue(self, monkeypatch):
        """开关关闭：视觉 0 次、识别队列 0 条、补识别队列 0 条。"""
        svc, db = make_svc()
        db.set_setting(KEY_IMAGE_VISION_ENABLED, "0")
        called = {"n": 0}

        def _never(data, **kw):
            called["n"] += 1
            return vehicle_result()

        monkeypatch.setattr(vision, "analyze_image", _never)
        svc._on_message(image_msg())
        assert called["n"] == 0
        assert svc.vision_queue_size == 0
        assert svc.image_retry_pending == 0
        error = skipped_logs(db)[0]["error"]
        # 文案主体保持历史口径（用户看到的就是「没做识别」）
        assert error.startswith("非文本消息（图片），不分析")
        assert "local_id=2467" in error

    def test_switch_takes_effect_without_restart(self, monkeypatch):
        """配置每条消息现读：改开关后无需重启进程即可生效。"""
        svc, db = make_svc()
        called = {"n": 0}

        def _fake(data, **kw):
            called["n"] += 1
            return vehicle_result()

        monkeypatch.setattr(svc, "get_image_bytes",
                            lambda u, l: _complete_jpeg())
        monkeypatch.setattr(vision, "analyze_image", _fake)
        db.set_setting(KEY_IMAGE_VISION_ENABLED, "0")
        svc._on_message(image_msg(2467))         # 关闭 → 不入队
        assert svc.vision_queue_size == 0
        db.set_setting(KEY_IMAGE_VISION_ENABLED, "1")
        with vision_workers(svc):
            svc._on_message(image_msg(2467))     # 同一个服务实例，开启即入队
            assert wait_processed(svc)
        assert called["n"] == 1

    def test_worker_rechecks_switch_before_vision(self, monkeypatch):
        """QA-P3-6：任务已在识别队列里时用户关掉开关，worker 不得再调模型。

        开关原来只在监听侧检查，排队中的任务被 worker 取出后仍会烧 1 次 token。
        """
        svc, db = make_svc()
        called = {"n": 0}

        def _never(data, **kw):
            called["n"] += 1
            return vehicle_result()

        monkeypatch.setattr(svc, "get_image_bytes",
                            lambda u, l: _complete_jpeg())
        monkeypatch.setattr(vision, "analyze_image", _never)
        # 模拟「任务已经排进识别队列」
        svc._vision_queue.put_nowait(_task(2467))
        db.set_setting(KEY_IMAGE_VISION_ENABLED, "0")   # 用户随后关掉开关
        with vision_workers(svc):
            assert wait_processed(svc)
        assert called["n"] == 0                  # 一次都没调视觉模型
        assert svc.image_retry_pending == 0      # 不入补识别队列
        assert db.get_image_analysis(USER, 2467) is None   # 不落库
        error = skipped_logs(db)[0]["error"]
        assert error.startswith("图片识别开关已关闭，跳过识别")
        assert "local_id=2467" in error


@pytest.fixture()
def status_env(tmp_path):
    """带 pipeline 替身的状态接口环境（/api/status 需要 queue 字段）。"""
    db = Database(str(tmp_path / "b7status.sqlite3"))
    svc = FakeWechatService()
    deps.configure(db=db, wechat=svc, pipeline=FakePipeline(), sender=None,
                   notifier=Notifier())
    app = FastAPI()
    for router in ALL_ROUTERS:
        app.include_router(router)
    client = TestClient(app)
    yield SimpleNamespace(db=db, client=client, svc=svc)
    deps.configure(db=None, wechat=None, pipeline=None, sender=None,
                   notifier=None)
    db.close()


class TestStatusVisionRetryPending:
    def test_defaults_to_zero_when_service_has_no_attribute(self, status_env):
        """向后兼容：服务实现没有该属性时按 0 返回，字段始终存在。"""
        body = status_env.client.get("/api/status").json()
        assert body["queue"]["vision_retry_pending"] == 0

    def test_reports_service_value(self, status_env):
        status_env.svc.image_retry_pending = 3
        body = status_env.client.get("/api/status").json()
        assert body["queue"]["vision_retry_pending"] == 3
