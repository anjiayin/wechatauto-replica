# -*- coding: utf-8 -*-
"""批次 8（第二轮反馈）：商机列表「消息原文」列只显示一条。

问题现场：多条消息在合并窗口里合成一条商机，AI 要点、详情都显示多条，
但商机列表的「消息原文」列（以及它的悬浮提示）只有一条——因为该列渲染的
是 ``key_quote``（AI 从原文里挑的一句），而不是 ``raw_text``（合并窗口的
全部消息行）。

修复：``list_opportunities`` 的 SELECT 补 ``raw_text`` 与 ``merged_count``，
``OpportunityListItem`` 同步暴露这两个字段，前端逐行渲染 + 悬浮显示全部。

本文件锁住后端契约（字段存在且内容完整），前端渲染由 tsc + 人工验证覆盖。
"""
from __future__ import annotations

from app.api import deps
from app.db import Database
from app.notifier import Notifier
from app.schemas import OpportunityListItem
from tests.test_api import env  # noqa: F401  pytest 夹具复用


RAW = ("[20:32] 84640-T6A-003ZA-YT\n"
       "[20:33] E95200195\n"
       "[20:33] 51441-07060")


def _seed(db: Database) -> dict:
    return db.insert_opportunity(
        session_username="12345@chatroom", session_name="拆车件群",
        sender_wxid="wxid_a", sender_name="张三",
        window_start="2026-11-12T20:32:00", window_end="2026-11-12T20:33:00",
        merged_count=3, raw_text=RAW,
        type_id=None, type_name="汽车配件", summary="三个件号",
        key_quote="51441-07060", confidence=85)


class TestListReturnsRawText:
    def test_db_list_carries_all_lines(self, tmp_path):
        db = Database(str(tmp_path / "t.sqlite3"))
        _seed(db)
        _total, rows = db.list_opportunities()
        assert rows[0]["raw_text"] == RAW
        assert rows[0]["merged_count"] == 3
        # key_quote 仍然只有一条（AI 摘录），两个字段语义不同
        assert rows[0]["key_quote"] == "51441-07060"
        db.close()

    def test_schema_exposes_fields(self):
        item = OpportunityListItem(id=1, key_quote="a", raw_text="b\nc",
                                   merged_count=2)
        assert item.raw_text == "b\nc"
        assert item.merged_count == 2

    def test_api_list_includes_raw_text(self, env):
        _seed(env.db)
        resp = env.client.get("/api/opportunities")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        # 前端「消息原文」列按行渲染的就是它：必须含全部 3 条
        assert item["raw_text"].splitlines() == RAW.splitlines()
        assert item["merged_count"] == 3
        assert item["key_quote"] == "51441-07060"

    def test_detail_still_has_raw_text(self, env):
        """详情本来就带 raw_text，确认加字段后未破坏详情契约。"""
        opp = _seed(env.db)
        resp = env.client.get(f"/api/opportunities/{opp['id']}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["raw_text"] == RAW
        assert "raw_text_display" in body
