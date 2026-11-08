# -*- coding: utf-8 -*-
"""QA 独立验证测试（fix5 五项用户反馈改进的第二轮交叉验证）。

与 test_feedback_fixes.py 互补，重点覆盖工程师测试未覆盖的场景：
- 迁移数据完整性（含回复字段/全部列值）、AUTOINCREMENT 连续性、
  新装库跳过、迁移中断残留（opportunities_new）后的自恢复；
- handled 状态走 HTTP PATCH 全链路（API → DB → 列表/筛选/恢复）；
- 发送者前缀正则的误伤反例（多行消息、链接冒号、URL 开头）；
- session_display / input_text_display 的空值与解析失败回退。

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_qa_fix5.py -v
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.router_opportunities import router as opp_router
from app.db import Database, _OPPORTUNITIES_COLUMNS
from app.notifier import Notifier
from app.wechat_service import WeChatService
from app.aggregator import Aggregator

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

LEGACY_CHECKIN = ("'pending','replied','ignored','handled'",
                  "'pending','replied','ignored'")


def make_legacy_db(path: str) -> dict:
    """构造旧版结构库：旧 CHECK + 一条含完整字段值的历史商机。

    Returns:
        {"opp_id": int, "expect": dict} — 迁移后应完整保留的字段快照。
    """
    db = Database(path)
    opp = db.insert_opportunity(
        session_username="111@chatroom", session_name="加工群",
        sender_wxid="wxid_a", sender_name="张三",
        window_start="2026-11-02T14:02:00", window_end="2026-11-02T14:05:00",
        merged_count=3, raw_text="[14:02] 原文A\n[14:04] 原文B",
        type_id=None, type_name="机械加工订单",
        summary="要点1\n要点2", key_quote="原文B", confidence=88)
    db.update_opportunity_status(opp["id"], "replied")
    db.set_opportunity_replied(opp["id"], "好的，已安排")
    expect = db.get_opportunity(opp["id"])
    # 降级为旧 CHECK 结构（模拟升级前库）
    old_columns = _OPPORTUNITIES_COLUMNS.replace(*LEGACY_CHECKIN)
    conn = db._conn
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("CREATE TABLE opportunities_legacy (%s)" % old_columns)
    conn.execute("INSERT INTO opportunities_legacy SELECT * FROM opportunities")
    conn.execute("DROP TABLE opportunities")
    conn.execute("ALTER TABLE opportunities_legacy RENAME TO opportunities")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    db.close()
    return {"opp_id": opp["id"], "expect": expect}


class TestMigrationIntegrity:
    """迁移（修复 2 前置）：旧库升级的数据完整性与健壮性。"""

    def test_all_fields_preserved_after_migration(self, tmp_path):
        """迁移后历史商机的全部列值逐一保留（含回复字段）。"""
        path = str(tmp_path / "mig_full.sqlite3")
        legacy = make_legacy_db(path)
        db = Database(path)
        after = db.get_opportunity(legacy["opp_id"])
        assert after is not None
        for key, value in legacy["expect"].items():
            assert after[key] == value, f"字段 {key} 迁移后不一致"
        db.close()

    def test_migration_idempotent_third_open(self, tmp_path):
        """迁移后再两次打开：不重跑、数据不变（幂等）。"""
        path = str(tmp_path / "mig_idem.sqlite3")
        legacy = make_legacy_db(path)
        db2 = Database(path)
        assert db2.get_opportunity(legacy["opp_id"])["status"] == "replied"
        db2.close()
        db3 = Database(path)
        row = db3._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='opportunities'").fetchone()
        assert "handled" in row["sql"]
        # 无迁移残留表
        leftover = db3._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'opportunities_%'").fetchall()
        assert leftover == []
        assert db3.get_opportunity(legacy["opp_id"])["status"] == "replied"
        db3.close()

    def test_fresh_db_skips_migration(self, tmp_path):
        """新装库（DDL 已含 handled）：不建迁移中间表，直接可用。"""
        db = Database(str(tmp_path / "fresh.sqlite3"))
        row = db._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='opportunities'").fetchone()
        assert "handled" in row["sql"]
        leftover = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE name='opportunities_new'"
        ).fetchall()
        assert leftover == []
        db.close()

    def test_autoincrement_continues_after_migration(self, tmp_path):
        """迁移后新插入商机的 id 不与历史 id 冲突（AUTOINCREMENT 连续）。"""
        path = str(tmp_path / "mig_ai.sqlite3")
        legacy = make_legacy_db(path)
        db = Database(path)
        new_opp = db.insert_opportunity(
            session_username="222@chatroom", session_name="群2",
            sender_wxid="wxid_b", sender_name="李四",
            window_start="2026-11-02T15:00:00", window_end="2026-11-02T15:01:00",
            merged_count=1, raw_text="新消息", type_id=None, type_name="t",
            summary="s", key_quote="", confidence=70)
        assert new_opp["id"] > legacy["opp_id"]
        db.close()

    def test_recovers_from_interrupted_migration(self, tmp_path):
        """迁移中断残留场景：旧表 + 空的 opportunities_new 残留 → 重启应能自恢复。"""
        path = str(tmp_path / "mig_crash.sqlite3")
        legacy = make_legacy_db(path)
        # 模拟上次迁移在 CREATE TABLE opportunities_new 后崩溃
        crash = Database.__new__(Database)  # 不触发迁移的裸连接
        import sqlite3
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE opportunities_new (%s)"
                     % _OPPORTUNITIES_COLUMNS)
        conn.commit()
        conn.close()
        del crash
        # 重启（修复后应清理残留并完成迁移）
        db = Database(path)
        assert db.get_opportunity(legacy["opp_id"])["status"] == "replied"
        assert db.update_opportunity_status(
            legacy["opp_id"], "handled") == 1
        db.close()


class TestHandledApiFlow:
    """修复 2：handled 状态经 HTTP PATCH 的全链路。"""

    @pytest.fixture()
    def env(self, tmp_path):
        db = Database(str(tmp_path / "api_fix5.sqlite3"))
        notifier = Notifier()
        deps.configure(db=db, wechat=None, pipeline=None,
                       sender=None, notifier=notifier)
        app = FastAPI()
        app.include_router(opp_router)
        client = TestClient(app)
        yield SimpleNamespace(db=db, client=client)
        deps.configure(db=None, wechat=None, pipeline=None,
                       sender=None, notifier=None)
        db.close()

    def _insert(self, db: Database) -> dict:
        return db.insert_opportunity(
            session_username="111@chatroom", session_name="加工群",
            sender_wxid="wxid_a", sender_name="张三",
            window_start="2026-11-02T14:02:00", window_end="2026-11-02T14:05:00",
            merged_count=1, raw_text="原文", type_id=None, type_name="t",
            summary="要点", key_quote="", confidence=80)

    def test_patch_handled_then_visible_then_restore(self, env):
        opp = self._insert(env.db)
        opp_id = opp["id"]
        # PATCH → handled
        resp = env.client.patch(f"/api/opportunities/{opp_id}",
                                json={"status": "handled"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "handled"
        # 默认列表（status 空）可见 handled
        resp = env.client.get("/api/opportunities")
        statuses = [it["status"] for it in resp.json()["items"]]
        assert "handled" in statuses
        # 精确筛选 handled
        resp = env.client.get("/api/opportunities", params={"status": "handled"})
        assert resp.json()["total"] == 1
        # 恢复 pending
        resp = env.client.patch(f"/api/opportunities/{opp_id}",
                                json={"status": "pending"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"
        assert env.db.get_opportunity(opp_id)["status"] == "pending"

    def test_patch_rejects_unknown_status(self, env):
        opp = self._insert(env.db)
        resp = env.client.patch(f"/api/opportunities/{opp['id']}",
                                json={"status": "done"})
        assert resp.status_code == 422
        assert env.db.get_opportunity(opp["id"])["status"] == "pending"

    def test_patch_nonexistent_404(self, env):
        resp = env.client.patch("/api/opportunities/99999",
                                json={"status": "handled"})
        assert resp.status_code == 404


class TestSenderPrefixEdgeCases:
    """修复 5：前缀正则的误伤反例（显示渲染侧 + 提取侧）。"""

    def test_multiline_message_extracts_prefix(self):
        """多行群消息（ID:\n行1\n行2）仍应提取权威发送者前缀。"""
        assert WeChatService.extract_content_sender(
            "wxid_a:\n第一行\n第二行") == "wxid_a"

    def test_url_body_without_prefix_not_misextracted(self):
        """无前缀且正文以 URL 开头：不应把 'http' 误当发送者 ID。"""
        assert WeChatService.extract_content_sender(
            "http://example.com 求购") == ""

    def test_display_render_multiline_keeps_prefix_lines(self):
        """显示渲染侧逐行解析：多行消息的前缀行正常替换为显示名。"""
        db = Database(":memory:")
        svc = WeChatService(db, Aggregator(lambda u: None))
        svc._wxdb = _FakeWxDb()
        text = "[14:02] wxid_zhang:\n第一行\n第二行"
        _, display = svc.build_log_displays("111@chatroom", text)
        assert "[14:02] 张三(群昵称):" in display
        assert "第一行" in display and "第二行" in display
        db.close()

    def test_display_colon_body_not_rewritten(self):
        """正文行含冒号（链接：xxx）但非成员/联系人 → 整行保留原值。"""
        db = Database(":memory:")
        svc = WeChatService(db, Aggregator(lambda u: None))
        svc._wxdb = _FakeWxDb()
        text = "[14:02] 链接：https://example.com"
        _, display = svc.build_log_displays("111@chatroom", text)
        assert display == text
        db.close()

    def test_resolve_sender_group_multiline_falls_back(self, tmp_path):
        """多行群消息：extract 失败时回退 sender_username（当前实现行为）。"""
        db = Database(":memory:")
        svc = WeChatService(db, Aggregator(lambda u: None))
        svc._wxdb = _FakeWxDb()
        msg = {"username": "111@chatroom", "type": "文本", "sender_id": 3,
               "sender_username": "wxid_wrong", "create_time": 1,
               "content": "wxid_zhang:\n第一行\n第二行"}
        wxid, name = svc._resolve_sender("111@chatroom", "group", msg)
        # 期望权威前缀 wxid_zhang（当前实现回退 wxid_wrong —— BUG-1 的表现）
        assert wxid == "wxid_zhang"
        assert name == "张三(群昵称)"
        db.close()


class _FakeWxDb:
    """最小微信库替身（群成员 + 昵称）。"""

    def get_group_members(self, chatroom: str) -> list[dict]:
        if chatroom == "111@chatroom":
            return [{"username": "wxid_zhang", "nick_name": "张三(群昵称)",
                     "remark": "", "is_owner": True}]
        return []

    def get_nickname(self, username: str) -> str:
        return {"wxid_li": "李四(备注)", "111@chatroom": "加工群"}.get(
            username, "")


class TestLogDisplaysRobustness:
    """修复 4：展示字段空值/异常输入的回退。"""

    def test_empty_and_none_inputs(self):
        db = Database(":memory:")
        svc = WeChatService(db, Aggregator(lambda u: None))
        svc._wxdb = _FakeWxDb()
        assert svc.build_log_displays("", "") == ("", "")
        display, text = svc.build_log_displays(None, None)  # type: ignore[arg-type]
        assert display == "" and text == ""
        db.close()

    def test_session_display_remark_priority(self):
        db = Database(":memory:")
        svc = WeChatService(db, Aggregator(lambda u: None))
        svc._wxdb = _FakeWxDb()
        display, _ = svc.build_log_displays("111@chatroom", "正文")
        assert display == "加工群"
        db.close()

    def test_private_unknown_session_fallback(self):
        db = Database(":memory:")
        svc = WeChatService(db, Aggregator(lambda u: None))
        display, text = svc.build_log_displays("unknown_wxid", "原文")
        assert display == "unknown_wxid"
        assert text == "原文"
        db.close()


class TestPrivateSenderMissingId:
    """修复 5：私聊 sender_id 缺失/异常值的行为。"""

    def test_sender_id_absent(self):
        db = Database(":memory:")
        svc = WeChatService(db, Aggregator(lambda u: None))
        svc._wxdb = _FakeWxDb()
        msg = {"username": "wxid_li", "type": "文本", "create_time": 1,
               "content": "在吗", "sender_username": "wxid_bogus"}
        wxid, name = svc._resolve_sender("wxid_li", "friend", msg)
        assert wxid == "wxid_li"
        assert name == "李四(备注)"
        db.close()

    def test_sender_id_none(self):
        db = Database(":memory:")
        svc = WeChatService(db, Aggregator(lambda u: None))
        svc._wxdb = _FakeWxDb()
        msg = {"username": "wxid_li", "type": "文本", "create_time": 1,
               "content": "在吗", "sender_id": None,
               "sender_username": "wxid_bogus"}
        wxid, _ = svc._resolve_sender("wxid_li", "friend", msg)
        assert wxid == "wxid_li"
        db.close()

    def test_sender_id_two_shows_me(self):
        db = Database(":memory:")
        svc = WeChatService(db, Aggregator(lambda u: None))
        msg = {"username": "wxid_li", "type": "文本", "create_time": 1,
               "content": "我方消息", "sender_id": 2,
               "sender_username": "whatever"}
        wxid, name = svc._resolve_sender("wxid_li", "friend", msg)
        assert wxid == "" and name == "我"
        db.close()
