# -*- coding: utf-8 -*-
"""批次 11 独立验收测试（QA 严过关）。

与 ``test_batch11.py``（工程师自测）的区别：这里刻意**不复用**工程师的
fixture 与断言，改用更贴近生产的方式取证——

1. 迁移用「真实库结构快照」（含 direction / CHECK 含 handled / 索引）与
   多种历史变体（缺列、多列、缺 handled、已有 raw_refs）各跑一遍；
2. raw_refs 落库走真实 ``Pipeline._on_result``（新建 + 合并两条路径），
   而不是只测 ``_build_raw_refs`` / ``Database.merge_opportunity``；
3. 坏 JSON 容错逐个取值（``''`` / ``NULL`` / 非数组 / 非法）打接口；
4. 发送人解析用「能抛异常 / 能返回空」的恶意替身，验证绝不 500；
5. run.py 端口解析覆盖边界（空串、0、65536、负数、非数字、环境变量非法）。

运行：``python -m pytest tests/test_qa_b11_verify.py -q --basetemp=...``
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.aggregator import Aggregator, AnalysisUnit
from app.api import ALL_ROUTERS
from app.api.deps import get_db, get_notifier, get_wechat_service
from app.db import Database
from app.notifier import Notifier
from app.pipeline import Pipeline
from app.sender_display import clear_cache, resolve_sender_display
from app.wechat_service import WeChatService

# 真实库 opportunities 的建表语句快照（批次 10 末状态：有 direction，无
# raw_refs；CHECK 已含 handled）。用它造「真·历史库」比手写简化表更可信。
_REAL_SCHEMA_BEFORE_B11 = """
CREATE TABLE opportunities (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  session_username TEXT NOT NULL,
  session_name     TEXT NOT NULL,
  sender_wxid      TEXT NOT NULL DEFAULT '',
  sender_name      TEXT NOT NULL,
  window_start     TEXT NOT NULL,
  window_end       TEXT NOT NULL,
  merged_count     INTEGER NOT NULL DEFAULT 1,
  raw_text         TEXT NOT NULL,
  type_id          INTEGER REFERENCES opportunity_types(id) ON DELETE SET NULL,
  type_name        TEXT NOT NULL,
  summary          TEXT NOT NULL,
  key_quote        TEXT NOT NULL DEFAULT '',
  confidence       INTEGER NOT NULL DEFAULT 0,
  direction        TEXT NOT NULL DEFAULT '',
  status           TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','replied','ignored','handled')),
  reply_content    TEXT,
  replied_at       TEXT,
  created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
)
"""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _make_db(path: str, schema_sql: str, rows: list[dict],
             extra_ddl: str = "") -> None:
    """按给定建表语句 + 数据造一个「历史库」文件。"""
    conn = sqlite3.connect(path)
    conn.execute(schema_sql)
    conn.execute("CREATE INDEX idx_opp_created ON opportunities (created_at DESC)")
    conn.execute("CREATE INDEX idx_opp_status ON opportunities (status)")
    conn.execute("CREATE INDEX idx_opp_session ON opportunities (session_username)")
    if extra_ddl:
        conn.execute(extra_ddl)
    for row in rows:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO opportunities ({cols}) VALUES ({marks})",
                     tuple(row.values()))
    conn.commit()
    conn.close()


def _row_values(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(
        "SELECT * FROM opportunities ORDER BY id").fetchall()]


def _unit(*, lines, local_ids, kinds, has_image_sell=True,
          session="8888@chatroom", sender="wxid_seller",
          start: Optional[datetime] = None) -> AnalysisUnit:
    """构造分析单元（时间正序，window 覆盖本单元）。"""
    now = start or datetime.now()
    start_iso = now.isoformat(timespec="seconds")
    end_iso = (now + timedelta(seconds=30)).isoformat(timespec="seconds")
    return AnalysisUnit(
        session_username=session,
        session_name="配件交流群",
        session_kind="group",
        sender_wxid=sender,
        sender_name="卖家老王",
        window_start=start_iso,
        window_end=end_iso,
        merged_count=len(lines),
        lines=list(lines),
        local_ids=list(local_ids),
        line_kinds=list(kinds),
        has_image_sell=has_image_sell,
    )


_HIT_RESULT = {
    "hit": True, "type": "配件销售", "confidence": 90,
    "points": ["要前保险杠"], "quote": "有没有前保险杠",
    "direction": "buy", "prompt_tokens": 10, "completion_tokens": 5,
}


def _pipeline(db: Database) -> Pipeline:
    return Pipeline(db, Notifier(), worker_count=1, queue_max=10)


# ---------------------------------------------------------------------------
# 1. DB 迁移（高风险区：SELECT * → 共有列复制）
# ---------------------------------------------------------------------------
class TestMigrationOnRealSchema:
    def test_batch10_schema_gains_raw_refs_without_touching_data(
            self, tmp_path):
        """真实库结构（有 direction、CHECK 含 handled）→ 只加列不改数据。"""
        path = str(tmp_path / "real_schema.sqlite3")
        rows = [
            {"session_username": "1@chatroom", "session_name": "配件群",
             "sender_name": "张三", "window_start": "2026-11-01T10:00:00",
             "window_end": "2026-11-01T10:01:00", "raw_text": "[10:00] 求货",
             "type_name": "求购", "summary": "要点", "status": "pending"},
            {"session_username": "1@chatroom", "session_name": "配件群",
             "sender_name": "李四", "window_start": "2026-11-02T10:00:00",
             "window_end": "2026-11-02T10:01:00", "raw_text": "[10:00] 已回复",
             "type_name": "求购", "summary": "要点", "status": "replied",
             "reply_content": "有货", "replied_at": "2026-11-02T10:05:00"},
            {"session_username": "wxid_x", "session_name": "王五",
             "sender_name": "王五", "window_start": "2026-11-03T10:00:00",
             "window_end": "2026-11-03T10:01:00", "raw_text": "[10:00] 忽略",
             "type_name": "求购", "summary": "要点", "status": "ignored"},
            {"session_username": "2@chatroom", "session_name": "另一个群",
             "sender_name": "赵六", "window_start": "2026-11-04T10:00:00",
             "window_end": "2026-11-04T10:01:00", "raw_text": "[10:00] 自行处理",
             "type_name": "配件销售", "summary": "要点", "status": "handled",
             "direction": "sell"},
        ]
        _make_db(path, _REAL_SCHEMA_BEFORE_B11, rows)
        conn = sqlite3.connect(path)
        before = _row_values(conn)
        conn.close()
        assert "raw_refs" not in before[0]

        db = Database(path)          # 触发幂等迁移
        db.close()

        conn = sqlite3.connect(path)
        after = _row_values(conn)
        conn.close()
        assert len(after) == len(before)
        for old, new in zip(before, after):
            assert new["raw_refs"] == "[]"
            for key, value in old.items():
                if key == "created_at":
                    # created_at 由既有迁移 _migrate_created_at_localtime
                    # （反馈 5：UTC → 本地）负责平移，与批次 11 无关
                    continue
                assert new[key] == value, f"列 {key} 被迁移改写"
        # handled 状态必须原样保留（工程师改动了这条迁移的复制逻辑）
        assert [r["status"] for r in after] == [
            "pending", "replied", "ignored", "handled"]
        assert after[3]["direction"] == "sell"
        assert after[1]["reply_content"] == "有货"

    def test_rebuild_path_preserves_all_statuses(self, tmp_path):
        """最老的库（CHECK 无 handled、缺 direction/raw_refs）走表重建后不丢行。"""
        path = str(tmp_path / "oldest.sqlite3")
        old_schema = _REAL_SCHEMA_BEFORE_B11.replace(
            "  direction        TEXT NOT NULL DEFAULT '',\n", "").replace(
            "CHECK (status IN ('pending','replied','ignored','handled'))",
            "CHECK (status IN ('pending','replied','ignored'))")
        assert "handled" not in old_schema and "direction" not in old_schema
        rows = [
            {"session_username": "1@chatroom", "session_name": "群",
             "sender_name": "A", "window_start": "2026-10-01T09:00:00",
             "window_end": "2026-10-01T09:01:00", "raw_text": "[09:00] 一",
             "type_name": "求购", "summary": "p1", "status": "pending"},
            {"session_username": "1@chatroom", "session_name": "群",
             "sender_name": "B", "window_start": "2026-10-02T09:00:00",
             "window_end": "2026-10-02T09:01:00", "raw_text": "[09:00] 二",
             "type_name": "求购", "summary": "p2", "status": "replied"},
            {"session_username": "1@chatroom", "session_name": "群",
             "sender_name": "C", "window_start": "2026-10-03T09:00:00",
             "window_end": "2026-10-03T09:01:00", "raw_text": "[09:00] 三",
             "type_name": "求购", "summary": "p3", "status": "ignored"},
        ]
        _make_db(path, old_schema, rows)

        db = Database(path)
        db.close()

        conn = sqlite3.connect(path)
        after = _row_values(conn)
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='opportunities'").fetchone()[0]
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='opportunities'")}
        conn.close()
        assert "handled" in ddl, "重建后 CHECK 应支持 handled"
        assert [r["status"] for r in after] == ["pending", "replied", "ignored"]
        assert all(r["raw_refs"] == "[]" for r in after)
        assert all(r["direction"] == "" for r in after)
        assert {"idx_opp_created", "idx_opp_status",
                "idx_opp_session"} <= idx, "重建后索引应恢复"

    def test_rebuild_keeps_existing_raw_refs(self, tmp_path):
        """CHECK 无 handled 但表里已有 raw_refs：重建时必须一起复制。"""
        path = str(tmp_path / "mid.sqlite3")
        mid_schema = _REAL_SCHEMA_BEFORE_B11.replace(
            "CHECK (status IN ('pending','replied','ignored','handled'))",
            "CHECK (status IN ('pending','replied','ignored'))").replace(
            "  raw_text         TEXT NOT NULL,",
            "  raw_text         TEXT NOT NULL,\n"
            "  raw_refs         TEXT NOT NULL DEFAULT '[]',")
        refs = json.dumps([{"line": "[09:00] [图片] 识别：车辆/汽车配件",
                            "local_id": 31, "kind": "image"}],
                          ensure_ascii=False)
        _make_db(path, mid_schema, [
            {"session_username": "1@chatroom", "session_name": "群",
             "sender_name": "A", "window_start": "2026-10-01T09:00:00",
             "window_end": "2026-10-01T09:01:00", "raw_text": "[09:00] 一",
             "raw_refs": refs, "type_name": "求购", "summary": "p1"},
        ])
        db = Database(path)
        db.close()
        conn = sqlite3.connect(path)
        after = _row_values(conn)
        conn.close()
        assert json.loads(after[0]["raw_refs"])[0]["local_id"] == 31

    def test_extra_legacy_column_does_not_break_migration(self, tmp_path):
        """旧表多出一列（历史遗留）时迁移不能报错（SELECT * 时代的经典崩溃）。"""
        path = str(tmp_path / "extra.sqlite3")
        schema = _REAL_SCHEMA_BEFORE_B11.replace(
            "CHECK (status IN ('pending','replied','ignored','handled'))",
            "CHECK (status IN ('pending','replied','ignored'))").replace(
            "  created_at       TEXT NOT NULL DEFAULT "
            "(datetime('now','localtime'))",
            "  created_at       TEXT NOT NULL DEFAULT "
            "(datetime('now','localtime')),\n  legacy_flag INTEGER DEFAULT 0")
        _make_db(path, schema, [
            {"session_username": "1@chatroom", "session_name": "群",
             "sender_name": "A", "window_start": "2026-10-01T09:00:00",
             "window_end": "2026-10-01T09:01:00", "raw_text": "[09:00] 一",
             "type_name": "求购", "summary": "p1", "legacy_flag": 7},
        ])
        db = Database(path)   # 不应抛异常
        db.close()
        conn = sqlite3.connect(path)
        after = _row_values(conn)
        conn.close()
        assert after[0]["raw_text"] == "[09:00] 一"

    def test_migration_is_idempotent_three_times(self, tmp_path):
        path = str(tmp_path / "idem.sqlite3")
        _make_db(path, _REAL_SCHEMA_BEFORE_B11, [
            {"session_username": "1@chatroom", "session_name": "群",
             "sender_name": "A", "window_start": "2026-10-01T09:00:00",
             "window_end": "2026-10-01T09:01:00", "raw_text": "[09:00] 一",
             "type_name": "求购", "summary": "p1"},
        ])
        for _ in range(3):
            db = Database(path)
            db.close()
        conn = sqlite3.connect(path)
        after = _row_values(conn)
        conn.close()
        assert len(after) == 1
        assert after[0]["raw_refs"] == "[]"


# ---------------------------------------------------------------------------
# 2. raw_refs 端到端（真实 Pipeline._on_result）
# ---------------------------------------------------------------------------
class TestRawRefsEndToEnd:
    def test_new_opportunity_persists_image_refs(self, tmp_path):
        db = Database(str(tmp_path / "e2e-new.sqlite3"))
        pipe = _pipeline(db)
        unit = _unit(
            lines=[("10:00", "有没有前保险杠"),
                   ("10:01", "[图片] 识别：车辆/汽车配件（前保险杠）")],
            local_ids=[0, 131],
            kinds=["text", "image"],
        )
        pipe._on_result(unit, dict(_HIT_RESULT), 120,
                        {"session_username": unit.session_username,
                         "sender_wxid": unit.sender_wxid,
                         "msg_count": unit.merged_count,
                         "input_text": unit.merged_text()},
                        "test-model")
        total, items = db.list_opportunities(page=1, page_size=10)
        assert total == 1, items
        opp = db.get_opportunity(items[0]["id"])
        refs = json.loads(opp["raw_refs"])
        assert refs == [{"line": "[10:01] [图片] 识别：车辆/汽车配件（前保险杠）",
                         "local_id": 131, "kind": "image"}]
        # line 必须能在 raw_text 里原样找到（前端按行等值匹配的前提）
        assert refs[0]["line"] in opp["raw_text"].split("\n")
        db.close()

    def test_merge_path_appends_refs_in_correct_order(self, tmp_path):
        """先新建（1 图）再合并（2 图）：3 个 ref 都在、顺序与行一致、JSON 合法。"""
        db = Database(str(tmp_path / "e2e-merge.sqlite3"))
        pipe = _pipeline(db)
        base_log = {"session_username": "8888@chatroom",
                    "sender_wxid": "wxid_seller", "msg_count": 2,
                    "input_text": "x"}
        u1 = _unit(lines=[("10:00", "先说一句"),
                          ("10:01", "[图片] 识别：车辆/汽车配件（前保险杠）")],
                   local_ids=[0, 131], kinds=["text", "image"])
        pipe._on_result(u1, dict(_HIT_RESULT), 100, dict(base_log), "m")
        u2 = _unit(lines=[("10:05", "[图片] 识别：车辆/汽车配件（大灯）"),
                          ("10:06", "价格 200"),
                          ("10:07", "[图片] 识别：车辆/汽车配件（叶子板）")],
                   local_ids=[201, 0, 202],
                   kinds=["image", "text", "image"])
        pipe._on_result(u2, dict(_HIT_RESULT), 100, dict(base_log), "m")

        total, items = db.list_opportunities(page=1, page_size=10)
        assert total == 1, "应合并到同一条商机（同会话同发送人且 pending）"
        opp = db.get_opportunity(items[0]["id"])
        assert opp["merged_count"] == 5
        refs = json.loads(opp["raw_refs"])          # 必须仍是合法 JSON
        assert [r["local_id"] for r in refs] == [131, 201, 202]
        lines = opp["raw_text"].split("\n")
        assert len(lines) == 5
        for ref in refs:
            assert ref["line"] in lines, "ref.line 必须与某一行完全相等"
            assert ref["line"].startswith("[1")
        db.close()

    def test_duplicate_identical_lines_get_separate_refs(self, tmp_path):
        """两行文本完全相同但 local_id 不同：refs 必须各留一条（不按文本去重）。"""
        db = Database(str(tmp_path / "e2e-dup.sqlite3"))
        pipe = _pipeline(db)
        unit = _unit(
            lines=[("10:00", "[图片] 识别：车辆/汽车配件"),
                   ("10:00", "[图片] 识别：车辆/汽车配件")],
            local_ids=[11, 12], kinds=["image", "image"])
        pipe._on_result(unit, dict(_HIT_RESULT), 10,
                        {"session_username": unit.session_username,
                         "sender_wxid": unit.sender_wxid, "msg_count": 2,
                         "input_text": "x"}, "m")
        _, items = db.list_opportunities(page=1, page_size=5)
        refs = json.loads(db.get_opportunity(items[0]["id"])["raw_refs"])
        assert [r["local_id"] for r in refs] == [11, 12]
        db.close()

    def test_text_only_unit_keeps_empty_array(self, tmp_path):
        db = Database(str(tmp_path / "e2e-text.sqlite3"))
        pipe = _pipeline(db)
        unit = _unit(lines=[("10:00", "只有文字")], local_ids=[0],
                     kinds=["text"], has_image_sell=False)
        pipe._on_result(unit, dict(_HIT_RESULT), 10,
                        {"session_username": unit.session_username,
                         "sender_wxid": unit.sender_wxid, "msg_count": 1,
                         "input_text": "只有文字"}, "m")
        _, items = db.list_opportunities(page=1, page_size=5)
        assert db.get_opportunity(items[0]["id"])["raw_refs"] == "[]"
        db.close()


# ---------------------------------------------------------------------------
# 3. 接口容错与契约
# ---------------------------------------------------------------------------
class _Svc:
    """最小微信服务替身：详情/日志只需要 build_log_displays。"""

    def build_log_displays(self, session_username: str, text: str):
        return str(session_username or ""), str(text or "")

    def get_display_name(self, wxid: str) -> str:   # noqa: D401
        return ""


class _Dummy:
    """deps.configure 要求但本组用例用不到的单例（详情只用到 notifier）。"""


def _build_app(db, svc):
    """按生产方式组装路由，并把单例注入 deps。

    注意：``router_opportunities`` 里 ``get_notifier()`` 是**直接调用**
    （不是 ``Depends``），所以必须走 ``deps.configure``，dependency_overrides
    对它是无效的。
    """
    import app.api.deps as deps_mod

    notifier = Notifier()
    deps_mod.configure(db=db, wechat=svc, pipeline=_Dummy(), sender=_Dummy(),
                       notifier=notifier)
    app = FastAPI()
    for router in ALL_ROUTERS:
        app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_wechat_service] = lambda: svc
    return TestClient(app)


@pytest.fixture()
def api(tmp_path):
    db = Database(str(tmp_path / "api.sqlite3"))
    client = _build_app(db, _Svc())
    yield client, db
    client.close()
    db.close()


def _insert_opp(db: Database, **over) -> dict:
    kwargs = dict(
        session_username="8888@chatroom", session_name="配件群",
        sender_wxid="wxid_a", sender_name="张三",
        window_start="2026-11-01T10:00:00", window_end="2026-11-01T10:01:00",
        merged_count=2, raw_text="[10:00] 有没有前保险杠",
        type_id=None, type_name="求购", summary="要点", key_quote="有没有前保险杠",
        confidence=80, direction="buy",
    )
    kwargs.update(over)
    return db.insert_opportunity(**kwargs)


class TestDetailRawRefsRobustness:
    @pytest.mark.parametrize("bad", [
        "not-json", '{"a":1}', "", "[1,2,3]", '"str"', "  ", "null", "[]",
    ])
    def test_bad_raw_refs_never_500(self, api, bad):
        client, db = api
        opp = _insert_opp(db)
        with db._lock:      # 直接改库模拟手改/异常写入
            db._conn.execute("UPDATE opportunities SET raw_refs=? WHERE id=?",
                             (bad, opp["id"]))
            db._conn.commit()
        resp = client.get(f"/api/opportunities/{opp['id']}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["raw_refs"] == []

    def test_null_raw_refs_is_rejected_by_schema_and_tolerated_by_parser(self):
        """NULL 在库层就被 NOT NULL 挡住；解析层依旧容错（双保险）。"""
        from app.api.router_opportunities import _parse_raw_refs
        db = Database(":memory:")
        opp = _insert_opp(db)
        with pytest.raises(sqlite3.IntegrityError):
            with db._lock:
                db._conn.execute(
                    "UPDATE opportunities SET raw_refs=NULL WHERE id=?",
                    (opp["id"],))
        assert _parse_raw_refs(None) == []
        assert _parse_raw_refs("") == []
        assert _parse_raw_refs("[]") == []
        db.close()

    def test_dict_element_without_text_is_passed_through_but_frontend_guards(
            self, api):
        """元素缺 line 字符串时不 500（前端 typeof 守卫会跳过，不渲染脏数据）。"""
        client, db = api
        opp = _insert_opp(db, raw_refs='[{"line":1}]')
        resp = client.get(f"/api/opportunities/{opp['id']}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["raw_refs"] == [{"line": 1}]

    def test_detail_returns_list_not_string(self, api):
        client, db = api
        refs = json.dumps([{"line": "[10:00] [图片] 识别：车辆/汽车配件",
                            "local_id": 9, "kind": "image"}],
                          ensure_ascii=False)
        opp = _insert_opp(db, raw_refs=refs)
        body = client.get(f"/api/opportunities/{opp['id']}").json()
        assert isinstance(body["raw_refs"], list)
        assert body["raw_refs"][0]["local_id"] == 9

    def test_handled_opportunity_detail_ok(self, api):
        """历史 handled 状态商机：详情仍 200 且 raw_refs 为空列表。"""
        client, db = api
        opp = _insert_opp(db)
        db.update_opportunity_status(opp["id"], "handled")
        resp = client.get(f"/api/opportunities/{opp['id']}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "handled"
        assert body["raw_refs"] == []

    def test_list_item_contract_not_widened(self, api):
        """列表接口不得多出 raw_refs（契约不能被顺手放宽）。"""
        client, db = api
        _insert_opp(db)
        item = client.get("/api/opportunities").json()["items"][0]
        assert "raw_refs" not in item
        assert "raw_text_display" not in item


# ---------------------------------------------------------------------------
# 4. 发送人显示名 / 检索
# ---------------------------------------------------------------------------
class _BoomSvc:
    """所有解析都抛异常：接口必须优雅回退，不能 500。"""

    def build_log_displays(self, session_username: str, text: str):
        return str(session_username or ""), str(text or "")

    def get_display_name(self, wxid: str):
        raise RuntimeError("微信库不可用")


class _NameSvc:
    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping

    def build_log_displays(self, session_username: str, text: str):
        return str(session_username or ""), str(text or "")

    def get_display_name(self, wxid: str) -> str:
        return self.mapping.get(wxid, "")


def _api_with(tmp_path, svc) -> tuple[TestClient, Database]:
    db = Database(str(tmp_path / "sender.sqlite3"))
    return _build_app(db, svc), db


class TestSenderDisplay:
    def setup_method(self):
        clear_cache()

    def test_group_card_beats_contact_name(self, tmp_path):
        db = Database(str(tmp_path / "card.sqlite3"))
        db.save_group_card("111@chatroom", "wxid_a", "群名片-老王")
        clear_cache()
        name = resolve_sender_display(db, lambda w: "联系人昵称",
                                      "111@chatroom", "wxid_a")
        assert name == "群名片-老王"
        db.close()

    def test_private_chat_ignores_group_cards(self, tmp_path):
        db = Database(str(tmp_path / "priv.sqlite3"))
        db.save_group_card("111@chatroom", "wxid_a", "群名片-老王")
        clear_cache()
        assert resolve_sender_display(db, lambda w: "联系人昵称",
                                      "wxid_a", "wxid_a") == "联系人昵称"
        db.close()

    def test_falls_back_to_wxid_when_all_layers_fail(self, tmp_path):
        db = Database(str(tmp_path / "fallback.sqlite3"))
        clear_cache()
        assert resolve_sender_display(db, None, "1@chatroom", "wxid_z") == "wxid_z"
        assert resolve_sender_display(
            db, lambda w: (_ for _ in ()).throw(RuntimeError()),
            "1@chatroom", "wxid_z") == "wxid_z"
        db.close()

    def test_empty_wxid_returns_empty(self, tmp_path):
        db = Database(str(tmp_path / "empty.sqlite3"))
        assert resolve_sender_display(db, None, "1@chatroom", "") == ""
        assert resolve_sender_display(db, None, "1@chatroom", "   ") == ""
        db.close()

    def test_db_failure_does_not_raise(self, tmp_path):
        db = Database(str(tmp_path / "dbfail.sqlite3"))

        def boom(_chatroom):
            raise RuntimeError("库炸了")

        db.get_group_cards = boom       # type: ignore[assignment]
        clear_cache()
        assert resolve_sender_display(
            db, lambda w: "昵称", "1@chatroom", "wxid_q") == "昵称"
        db.close()

    def test_broken_wechat_service_never_500(self, tmp_path):
        """真实场景：微信库不可用 → 回退 wxid，日志接口不能 500。"""
        client, db = _api_with(tmp_path, _BoomSvc())
        db.insert_analysis_log(session_username="1@chatroom",
                               sender_wxid="wxid_broken", msg_count=1,
                               input_text="hi", status="ok")
        resp = client.get("/api/analysis-logs")
        assert resp.status_code == 200, resp.text
        assert resp.json()["items"][0]["sender_display"] == "wxid_broken"
        # 带筛选也不能 500
        resp2 = client.get("/api/analysis-logs", params={"sender": "wxid_broken"})
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["total"] == 1
        client.close()
        db.close()

    def test_filter_by_group_card_name_and_combination(self, tmp_path):
        svc = _NameSvc({"wxid_a": "联系人A", "wxid_b": "联系人B"})
        client, db = _api_with(tmp_path, svc)
        db.save_group_card("111@chatroom", "wxid_a", "群昵称-小王")
        clear_cache()
        for i in range(3):
            db.insert_analysis_log(session_username="111@chatroom",
                                   sender_wxid="wxid_a", msg_count=1,
                                   input_text="群里的", status="ok")
        db.insert_analysis_log(session_username="222@chatroom",
                               sender_wxid="wxid_b", msg_count=1,
                               input_text="别的群", status="error")
        db.insert_analysis_log(session_username="wxid_b", sender_wxid="wxid_b",
                               msg_count=1, input_text="私聊", status="ok")

        body = client.get("/api/analysis-logs").json()
        names = {(i["session_username"], i["sender_display"])
                 for i in body["items"]}
        assert ("111@chatroom", "群昵称-小王") in names
        assert ("222@chatroom", "联系人B") in names
        assert ("wxid_b", "联系人B") in names

        body = client.get("/api/analysis-logs",
                          params={"sender": "群昵称-小王"}).json()
        assert body["total"] == 3
        assert all(i["sender_wxid"] == "wxid_a" for i in body["items"])

        body = client.get("/api/analysis-logs",
                          params={"sender": "联系人B", "status": "ok"}).json()
        assert body["total"] == 1
        assert body["items"][0]["input_text"] == "私聊"

        body = client.get("/api/analysis-logs",
                          params={"sender": "查无此人"}).json()
        assert body["total"] == 0 and body["items"] == []

        # 引号/通配符等异常输入不能 500（SQL 参数化 + LIKE 通配）
        for weird in ["'", "'; DROP TABLE analysis_logs;--", "%", "_"]:
            r = client.get("/api/analysis-logs", params={"sender": weird})
            assert r.status_code == 200, (weird, r.text)
        assert db.list_analysis_logs(page=1, page_size=1)[0] == 5
        client.close()
        db.close()

    def test_sender_candidate_limit_and_latency(self, tmp_path):
        """500 条候选上限下的响应时间（目标：单页 < 1s，纯本地库读取）。"""
        client, db = _api_with(tmp_path, _NameSvc({}))
        for idx in range(600):
            db.insert_analysis_log(
                session_username=f"{idx % 30}@chatroom",
                sender_wxid=f"wxid_user{idx}", msg_count=1,
                input_text=f"msg {idx}", status="ok")
        assert len(db.distinct_log_sender_pairs(limit=500)) == 500
        t0 = time.perf_counter()
        resp = client.get("/api/analysis-logs", params={"sender": "wxid_user59"})
        cost = time.perf_counter() - t0
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1
        assert cost < 1.0, f"候选 500 条时筛选耗时 {cost:.3f}s"
        client.close()
        db.close()


# ---------------------------------------------------------------------------
# 5. 端口可配置
# ---------------------------------------------------------------------------
class TestRunAddress:
    def _resolve(self, argv=None, env=None, monkeypatch=None):
        import run as run_mod
        if env:
            for key, value in env.items():
                monkeypatch.setenv(key, value)
        return run_mod.resolve_address(argv)

    def test_default(self, monkeypatch):
        monkeypatch.delenv("KEFU_PORT", raising=False)
        monkeypatch.delenv("KEFU_HOST", raising=False)
        assert self._resolve([], None, monkeypatch) == ("127.0.0.1", 8765)

    def test_env_port(self, monkeypatch):
        assert self._resolve([], {"KEFU_PORT": "8793"}, monkeypatch)[1] == 8793

    def test_cli_port_beats_env(self, monkeypatch):
        assert self._resolve(["--port", "8792"],
                             {"KEFU_PORT": "8793"}, monkeypatch)[1] == 8792

    def test_host_env_and_cli(self, monkeypatch):
        monkeypatch.delenv("KEFU_HOST", raising=False)
        assert self._resolve([], {"KEFU_HOST": "0.0.0.0"}, monkeypatch)[0] \
            == "0.0.0.0"
        assert self._resolve(["--host", "192.168.1.5"],
                             {"KEFU_HOST": "0.0.0.0"}, monkeypatch)[0] \
            == "192.168.1.5"

    @pytest.mark.parametrize("bad", ["70000", "0", "-1", "abc", "12.5", "8765abc"])
    def test_invalid_cli_port_exit_2(self, bad, monkeypatch):
        with pytest.raises(SystemExit) as exc:
            self._resolve(["--port", bad], None, monkeypatch)
        assert exc.value.code == 2

    def test_explicit_empty_cli_port_falls_back_to_default(self, monkeypatch):
        """``--port ""`` 目前等价于「未指定」→ 默认 8765（非报错，记录行为）。"""
        monkeypatch.delenv("KEFU_PORT", raising=False)
        assert self._resolve(["--port", ""], None, monkeypatch)[1] == 8765

    def test_invalid_env_port_exit_2(self, monkeypatch):
        with pytest.raises(SystemExit) as exc:
            self._resolve([], {"KEFU_PORT": "abc"}, monkeypatch)
        assert exc.value.code == 2

    def test_empty_env_port_uses_default(self, monkeypatch):
        assert self._resolve([], {"KEFU_PORT": ""}, monkeypatch)[1] == 8765


# ---------------------------------------------------------------------------
# 6. Aggregator 行类型/ID 对齐（错位是 raw_refs 张冠李戴的根因）
# ---------------------------------------------------------------------------
class TestAggregatorAlignment:
    def _collect(self) -> tuple[Aggregator, list]:
        units: list[AnalysisUnit] = []
        agg = Aggregator(flush_callback=units.append,
                         window_sec_provider=lambda: 5)
        return agg, units

    @staticmethod
    def _msg(text: str, *, local_id: int = 0, image_sell: bool = False,
             sender: str = "wxid_s") -> dict:
        return {"username": "1@chatroom", "content": text,
                "local_id": local_id, "image_sell": image_sell,
                "sender_username": sender, "sender_display_name": "老王",
                "create_time": datetime.now().timestamp()}

    def test_kinds_and_ids_stay_aligned(self):
        agg, units = self._collect()
        agg.add(self._msg("求前杠"), "群", "group")
        agg.add(self._msg("[图片] 识别：车辆/汽车配件（前杠）",
                          local_id=131, image_sell=True), "群", "group")
        agg.add(self._msg("多少钱"), "群", "group")
        agg._flush_expired(force=True)
        assert len(units) == 1
        unit = units[0]
        assert unit.line_kinds == ["text", "image", "text"]
        assert unit.local_ids == [0, 131, 0]
        assert len(unit.lines) == 3
        refs = json.loads(Pipeline._build_raw_refs(unit))
        assert [r["local_id"] for r in refs] == [131]
        assert refs[0]["line"] == "[%s] [图片] 识别：车辆/汽车配件（前杠）" % unit.lines[1][0]

    def test_empty_message_does_not_desync_lists(self):
        agg, units = self._collect()
        assert agg.add(self._msg("   "), "群", "group") is False
        agg.add(self._msg("有效"), "群", "group")
        agg._flush_expired(force=True)
        unit = units[0]
        assert len(unit.lines) == len(unit.local_ids) == len(unit.line_kinds) == 1

    def test_group_sender_prefix_stripped_before_refs(self):
        agg, units = self._collect()
        agg.add(self._msg("wxid_abc:" + chr(10) + "[图片] 识别：车辆/汽车配件",
                          local_id=77, image_sell=True), "群", "group")
        agg._flush_expired(force=True)
        unit = units[0]
        assert unit.lines[0][1] == "[图片] 识别：车辆/汽车配件"
        refs = json.loads(Pipeline._build_raw_refs(unit))
        assert refs[0]["line"] == "[%s] [图片] 识别：车辆/汽车配件" % unit.lines[0][0]

    def test_missing_line_kinds_falls_back_to_text_detection(self):
        """外部直接构造的旧式 unit（无 line_kinds）也不能静默丢图。"""
        unit = _unit(lines=[("10:00", "先说话"),
                            ("10:01", "[图片] 识别：车辆/汽车配件")],
                     local_ids=[0, 55], kinds=[])
        refs = json.loads(Pipeline._build_raw_refs(unit))
        assert [r["local_id"] for r in refs] == [55]

    def test_part_name_with_newline_breaks_line_matching(self):
        """部件名内含换行：修复后 `_image_sell_text` 归一空白 → 单行（M1）。

        修复前这里是「已知缺陷留证」（refs.line 带 \\n，前端按行等值
        匹配永远漏图）。修复层在 `wechat_service._image_sell_text`：
        连续空白（含 \\n/\\r/全角空格）归一成单个半角空格，因此进入
        聚合窗口的图片行一定是单行，raw_refs 能与原文行等值命中。
        """
        text = WeChatService._image_sell_text(
            {"part_name": "前杠\n左", "vin": "LFV3A24F2A3102185"})
        assert text == "[图片] 识别：车辆/汽车配件（前杠 左） 车架号：LFV3A24F2A3102185"
        # 全角空格 / \r\n 同样归一
        text2 = WeChatService._image_sell_text(
            {"part_name": "保险杠\r\n\u3000（左）", "vin": ""})
        assert "\n" not in text2 and "\r" not in text2 and "\u3000" not in text2
        assert text2 == "[图片] 识别：车辆/汽车配件（保险杠 （左））"

    def test_local_id_conversion_failure_keeps_arrays_aligned(self):
        """local_id 非数字（L1）：转换失败兜 0，三数组严格同序不张冠李戴。"""
        agg, units = self._collect()
        agg.add(self._msg("第一条", local_id="x"), "群", "group")
        agg.add(self._msg("[图片] 识别：车辆/汽车配件（前杠）",
                          local_id=131, image_sell=True), "群", "group")
        agg._flush_expired(force=True)
        unit = units[0]
        assert len(unit.lines) == len(unit.local_ids) == len(unit.line_kinds) == 2
        assert unit.local_ids == [0, 131]
        assert unit.line_kinds == ["text", "image"]
        refs = json.loads(Pipeline._build_raw_refs(unit))
        assert [r["local_id"] for r in refs] == [131]
