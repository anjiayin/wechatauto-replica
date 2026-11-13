# -*- coding: utf-8 -*-
"""批次 11 回归测试：原文图片真图 / 日志发送人显示与检索 / 启动端口可配置。

覆盖：

- 需求 1（Bug）商机详情「消息原文」里图片没显示：
  Aggregator 行类型（``line_kinds``）与 lines/local_ids 同序 →
  pipeline ``_build_raw_refs`` 只收图片行且 line 带 ``[HH:MM] `` 前缀 →
  ``opportunities.raw_refs`` 落库（新库 DDL / 老库幂等加列）→
  ``merge_opportunity`` 在 SQL 端拼接数组（三种边界都是合法 JSON）→
  详情接口把 JSON 解析成 list（坏 JSON 容错为 []，绝不 500）。
- 需求 2 分析列表「来源会话」加发送人并可检索：
  显示名优先级（群昵称 > 联系人昵称 > wxid）、群名片进程内缓存、
  ``sender`` 筛选（昵称 / wxid 片段 / 无命中）、``distinct_log_senders``、
  单条解析失败只跳过该条。
- 需求 3 启动端口可配置：命令行 > 环境变量 > 默认 127.0.0.1:8765，
  非法端口以退出码 2 结束。

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_batch11.py -v --basetemp=D:/tmp/kefu-b11-1
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import run as run_module
from app.aggregator import Aggregator, AnalysisUnit
from app.api import ALL_ROUTERS, deps
from app.db import Database
from app.notifier import Notifier
from app.pipeline import Pipeline
from app.sender_display import clear_cache, resolve_sender_display
from tests.test_api import FakePipeline, FakeSender, FakeWechatService
from tests.test_core import FakeChatClient

GROUP = "111@chatroom"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def make_unit(*, lines, local_ids=None, kinds=None,
              image_sell: bool = True) -> AnalysisUnit:
    """构造一个分析单元（lines 为 (HH:MM, 内容) 列表）。"""
    return AnalysisUnit(
        session_username=GROUP,
        session_name="配件群",
        session_kind="group",
        sender_wxid="wxid_a",
        sender_name="张三",
        window_start="2026-11-02T20:32:00",
        window_end="2026-11-02T20:33:00",
        merged_count=len(lines),
        lines=list(lines),
        local_ids=list(local_ids if local_ids is not None
                       else [0] * len(lines)),
        line_kinds=list(kinds if kinds is not None
                        else ["text"] * len(lines)),
        has_image_sell=image_sell,
    )


def insert_opp(db: Database, *, raw_refs: str = "[]") -> dict:
    """插入一条商机（raw_refs 可指定）。"""
    return db.insert_opportunity(
        session_username=GROUP, session_name="配件群",
        sender_wxid="wxid_a", sender_name="张三",
        window_start="2026-11-02T20:32:00", window_end="2026-11-02T20:33:00",
        merged_count=1, raw_text="[20:32] [图片] 识别：车辆/汽车配件（前保险杠）",
        raw_refs=raw_refs,
        type_id=None, type_name="配件销售", summary="有前保险杠现货",
        key_quote="有前保险杠现货", confidence=80, direction="sell")


def merge(db: Database, opp_id: int, raw_refs_append: str) -> dict | None:
    """以最小参数做一次合并（只关心 raw_refs）。"""
    return db.merge_opportunity(
        opp_id, raw_text_append="[20:33] 还有大灯",
        summary_append="", window_end="2026-11-02T20:34:00",
        merged_count_delta=1, key_quote="还有大灯", confidence=70,
        type_id=None, type_name="配件销售",
        raw_refs_append=raw_refs_append)


@pytest.fixture(autouse=True)
def _reset_card_cache():
    """每个用例前后清空群名片缓存，避免用例间互相影响。"""
    clear_cache()
    yield
    clear_cache()


# ---------------------------------------------------------------------------
# 需求 1：Aggregator 行类型
# ---------------------------------------------------------------------------
class TestAggregatorLineKinds:
    def test_line_kinds_synced_with_lines_and_ids(self):
        """image_sell 的行标 image，其余标 text，三者严格同序。"""
        units: list[AnalysisUnit] = []
        agg = Aggregator(units.append, window_sec_provider=lambda: 5)
        agg._window_sec_provider = lambda: 5
        # 直接注入缓冲并强制 flush（不启动线程，避免测试等待）
        agg.add({"username": GROUP, "content": "我要前保险杠",
                 "sender_username": "wxid_a", "sender_display_name": "张三",
                 "create_time": time.time(), "local_id": 130},
                "配件群", "group")
        agg.add({"username": GROUP,
                 "content": "[图片] 识别：车辆/汽车配件（前保险杠）",
                 "sender_username": "wxid_a", "sender_display_name": "张三",
                 "create_time": time.time(), "local_id": 131,
                 "image_sell": True},
                "配件群", "group")
        agg.stop(flush_all=True)
        assert len(units) == 1
        unit = units[0]
        assert len(unit.lines) == 2
        assert unit.line_kinds == ["text", "image"]
        assert unit.local_ids == [130, 131]
        assert unit.has_image_sell is True

    def test_line_kinds_default_is_empty(self):
        """外部直接构造的 AnalysisUnit 不带 line_kinds 也不能炸。"""
        unit = make_unit(lines=[("20:32", "你好")])
        unit.line_kinds = []
        assert Pipeline._build_raw_refs(unit) == "[]"


# ---------------------------------------------------------------------------
# 需求 1：pipeline 构造 raw_refs
# ---------------------------------------------------------------------------
class TestBuildRawRefs:
    def test_only_image_lines_with_local_id(self):
        """只收图片行 + local_id>0；line 保留完整原文（含时间前缀）。"""
        unit = make_unit(
            lines=[("20:32", "我要前保险杠"),
                   ("20:32", "[图片] 识别：车辆/汽车配件（前保险杠）"),
                   ("20:33", "[图片] 识别：车辆/汽车配件（大灯）"),
                   ("20:33", "[图片] 识别：车辆/汽车配件（无 local_id）")],
            local_ids=[130, 131, 0, 0],
            kinds=["text", "image", "image", "image"],
        )
        refs = json.loads(Pipeline._build_raw_refs(unit))
        assert refs == [{
            "line": "[20:32] [图片] 识别：车辆/汽车配件（前保险杠）",
            "local_id": 131,
            "kind": "image",
        }]

    def test_text_only_returns_empty_array(self):
        unit = make_unit(lines=[("20:32", "我要前保险杠")], local_ids=[130])
        assert json.loads(Pipeline._build_raw_refs(unit)) == []

    def test_chinese_not_escaped(self):
        """ensure_ascii=False：库里存中文原文，不能是 \\uXXXX。"""
        unit = make_unit(lines=[("20:32", "[图片] 识别：车辆/汽车配件（前保险杠）")],
                         local_ids=[131], kinds=["image"])
        raw = Pipeline._build_raw_refs(unit)
        assert "前保险杠" in raw and "\\u" not in raw


# ---------------------------------------------------------------------------
# 需求 1：DB 层 raw_refs（DDL / 加列迁移 / 插入 / 合并拼接）
# ---------------------------------------------------------------------------
class TestDbRawRefs:
    def test_insert_default_empty(self):
        db = Database(":memory:")
        opp = db.insert_opportunity(
            session_username=GROUP, session_name="配件群",
            sender_wxid="wxid_a", sender_name="张三",
            window_start="2026-11-02T20:32:00",
            window_end="2026-11-02T20:33:00", merged_count=1,
            raw_text="[20:32] 你好", type_id=None, type_name="配件销售",
            summary="", key_quote="", confidence=60)
        assert opp["raw_refs"] == "[]"
        db.close()

    def test_insert_and_read_back(self):
        db = Database(":memory:")
        payload = json.dumps([{"line": "[20:32] [图片] 识别：车辆/汽车配件",
                               "local_id": 131, "kind": "image"}],
                             ensure_ascii=False)
        opp = insert_opp(db, raw_refs=payload)
        assert db.get_opportunity(opp["id"])["raw_refs"] == payload
        db.close()

    def test_merge_appends_valid_json_array(self):
        """合并追加后仍是合法 JSON 数组（顺序保持旧在前、新在后）。"""
        db = Database(":memory:")
        opp = insert_opp(db, raw_refs=json.dumps(
            [{"line": "[20:32] 图1", "local_id": 131, "kind": "image"}],
            ensure_ascii=False))
        merged = merge(db, opp["id"], json.dumps(
            [{"line": "[20:33] 图2", "local_id": 132, "kind": "image"}],
            ensure_ascii=False))
        refs = json.loads(merged["raw_refs"])
        assert [r["local_id"] for r in refs] == [131, 132]
        db.close()

    def test_merge_boundaries(self):
        """'[]' / '' / NULL / 追加为空 四种边界都拼成合法数组。"""
        db = Database(":memory:")
        new = json.dumps([{"line": "[20:33] 图2", "local_id": 132,
                           "kind": "image"}], ensure_ascii=False)

        # 旧值 '[]' → 直接用新值
        opp = insert_opp(db, raw_refs="[]")
        merged = merge(db, opp["id"], new)
        assert [r["local_id"] for r in json.loads(merged["raw_refs"])] == [132]

        # 旧值为空串（手改库/异常写入）→ 直接用新值
        opp2 = insert_opp(db, raw_refs="[]")
        with db._lock:
            db._conn.execute("UPDATE opportunities SET raw_refs='' WHERE id=?",
                             (opp2["id"],))
            db._conn.commit()
        merged = merge(db, opp2["id"], new)
        assert [r["local_id"] for r in json.loads(merged["raw_refs"])] == [132]

        # 旧值为 NULL（列是 NOT NULL，正常写不进来；SQL 侧仍按「无内容」
        # 兜底，这里只断言约束本身存在，防止后续 DDL 改成可空后行为漂移）
        cols = {r["name"]: r for r in db._conn.execute(
            "PRAGMA table_info(opportunities)").fetchall()}
        assert cols["raw_refs"]["notnull"] == 1
        assert cols["raw_refs"]["dflt_value"] == "'[]'"

        # 追加为空（'[]' 与 ''）→ 旧值原样保留、不变脏
        opp4 = insert_opp(db, raw_refs=json.dumps(
            [{"line": "[20:32] 图1", "local_id": 131, "kind": "image"}],
            ensure_ascii=False))
        for empty in ("[]", ""):
            merged = merge(db, opp4["id"], empty)
            assert json.loads(merged["raw_refs"]) == [
                {"line": "[20:32] 图1", "local_id": 131, "kind": "image"}]
        db.close()

    def test_old_db_migration_adds_column(self, tmp_path):
        """老库（无 raw_refs）打开后自动加列，存量行默认 '[]'。"""
        path = str(tmp_path / "old11.sqlite3")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE opportunities ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " session_username TEXT NOT NULL, session_name TEXT NOT NULL,"
            " sender_wxid TEXT NOT NULL DEFAULT '', sender_name TEXT NOT NULL,"
            " window_start TEXT NOT NULL, window_end TEXT NOT NULL,"
            " merged_count INTEGER NOT NULL DEFAULT 1, raw_text TEXT NOT NULL,"
            " type_id INTEGER, type_name TEXT NOT NULL, summary TEXT NOT NULL,"
            " key_quote TEXT NOT NULL DEFAULT '',"
            " confidence INTEGER NOT NULL DEFAULT 0,"
            " direction TEXT NOT NULL DEFAULT '',"
            " status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN"
            " ('pending','replied','ignored','handled')),"
            " reply_content TEXT, replied_at TEXT,"
            " created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')))")
        conn.execute(
            "INSERT INTO opportunities (session_username, session_name,"
            " sender_name, window_start, window_end, raw_text, type_name,"
            " summary) VALUES ('111@chatroom','配件群','张三',"
            " '2026-11-02T20:32:00','2026-11-02T20:33:00','[20:32] 老数据',"
            " '配件销售','要点')")
        conn.commit()
        conn.close()

        db = Database(path)   # 打开即触发幂等迁移
        cols = {r["name"] for r in db._conn.execute(
            "PRAGMA table_info(opportunities)").fetchall()}
        assert "raw_refs" in cols
        row = db._conn.execute("SELECT raw_refs FROM opportunities").fetchone()
        assert row["raw_refs"] == "[]"
        db.close()
        # 可重复执行（幂等）：再开一次不报错
        db2 = Database(path)
        db2.close()

    def test_very_old_db_rebuild_keeps_rows(self, tmp_path):
        """更老的库（status 无 handled 且缺 direction/raw_refs）重建后不丢数据。"""
        path = str(tmp_path / "older11.sqlite3")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE opportunities ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " session_username TEXT NOT NULL, session_name TEXT NOT NULL,"
            " sender_wxid TEXT NOT NULL DEFAULT '', sender_name TEXT NOT NULL,"
            " window_start TEXT NOT NULL, window_end TEXT NOT NULL,"
            " merged_count INTEGER NOT NULL DEFAULT 1, raw_text TEXT NOT NULL,"
            " type_id INTEGER, type_name TEXT NOT NULL, summary TEXT NOT NULL,"
            " key_quote TEXT NOT NULL DEFAULT '',"
            " confidence INTEGER NOT NULL DEFAULT 0,"
            " status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN"
            " ('pending','replied','ignored')),"
            " reply_content TEXT, replied_at TEXT,"
            " created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')))")
        conn.execute(
            "INSERT INTO opportunities (session_username, session_name,"
            " sender_name, window_start, window_end, raw_text, type_name,"
            " summary) VALUES ('111@chatroom','配件群','张三',"
            " '2026-11-02T20:32:00','2026-11-02T20:33:00','[20:32] 老数据',"
            " '配件销售','要点')")
        conn.commit()
        conn.close()

        db = Database(path)   # 表重建 + 加列迁移
        row = db._conn.execute(
            "SELECT raw_text, status, direction, raw_refs FROM opportunities"
        ).fetchone()
        assert row["raw_text"] == "[20:32] 老数据"
        assert row["status"] == "pending"
        assert row["direction"] == ""
        assert row["raw_refs"] == "[]"
        db.close()


# ---------------------------------------------------------------------------
# 需求 1：详情接口 raw_refs
# ---------------------------------------------------------------------------
@pytest.fixture()
def env(tmp_path):
    """真实路由 + 替身依赖 + 临时库（与既有测试同构）。"""
    db = Database(str(tmp_path / "batch11.sqlite3"))
    # 用带「联系人昵称映射」的替身：需求 2 要验证显示名解析优先级
    svc = DisplayFakeWechatService()
    pipeline = FakePipeline()
    sender = FakeSender()
    notifier = Notifier()
    deps.configure(db=db, wechat=svc, pipeline=pipeline,
                   sender=sender, notifier=notifier)
    app = FastAPI()
    for router in ALL_ROUTERS:
        app.include_router(router)
    client = TestClient(app)
    yield SimpleNamespace(db=db, svc=svc, pipeline=pipeline,
                          sender=sender, notifier=notifier, client=client)
    deps.configure(db=None, wechat=None, pipeline=None,
                   sender=None, notifier=None)
    db.close()


class TestDetailRawRefsApi:
    def test_detail_returns_parsed_refs(self, env):
        payload = json.dumps(
            [{"line": "[20:32] [图片] 识别：车辆/汽车配件（前保险杠）",
              "local_id": 131, "kind": "image"}], ensure_ascii=False)
        opp = insert_opp(env.db, raw_refs=payload)
        body = env.client.get(f"/api/opportunities/{opp['id']}").json()
        assert body["raw_refs"] == [{
            "line": "[20:32] [图片] 识别：车辆/汽车配件（前保险杠）",
            "local_id": 131, "kind": "image"}]

    def test_detail_default_empty_list(self, env):
        """存量商机（raw_refs='[]'）→ 空列表，行为与旧版一致。"""
        opp = insert_opp(env.db)
        body = env.client.get(f"/api/opportunities/{opp['id']}").json()
        assert body["raw_refs"] == []

    def test_detail_bad_json_tolerated(self, env):
        """库里是坏 JSON / 非数组 → 容错为 []，绝不 500。"""
        for bad in ("not-json", '{"a": 1}', "123"):
            opp = insert_opp(env.db, raw_refs=bad)
            resp = env.client.get(f"/api/opportunities/{opp['id']}")
            assert resp.status_code == 200
            assert resp.json()["raw_refs"] == []

    def test_list_item_has_no_raw_refs(self, env):
        """列表项不含 raw_refs（字段集合契约不变）。"""
        insert_opp(env.db)
        items = env.client.get("/api/opportunities").json()["items"]
        assert "raw_refs" not in items[0]


# ---------------------------------------------------------------------------
# 需求 2：发送人显示名解析
# ---------------------------------------------------------------------------
class DisplayFakeWechatService(FakeWechatService):
    """在 FakeWechatService 上补「username → 联系人昵称」映射。"""

    def __init__(self, names: dict[str, str] | None = None) -> None:
        super().__init__()
        self.names = names or {}
        self.display_error: set[str] = set()
        self.name_calls: list[str] = []

    def get_display_name(self, username: str) -> str:
        self.name_calls.append(username)
        if username in self.display_error:
            raise RuntimeError("显示名解析失败")
        return self.names.get(username, username)


class TestSenderDisplay:
    def test_group_card_priority(self, env):
        """群昵称（group_cards）优先于联系人昵称。"""
        env.svc.names["wxid_a"] = "张三（全局昵称）"
        env.db.save_group_card(GROUP, "wxid_a", "张三（群昵称）")
        assert resolve_sender_display(
            env.db, env.svc.get_display_name, GROUP, "wxid_a") == "张三（群昵称）"

    def test_fallback_to_contact_name_then_wxid(self, env):
        """无名片 → 联系人昵称；联系人也没有 → wxid 本身。"""
        env.svc.names["wxid_b"] = "李四"
        assert resolve_sender_display(
            env.db, env.svc.get_display_name, GROUP, "wxid_b") == "李四"
        assert resolve_sender_display(
            env.db, env.svc.get_display_name, GROUP, "wxid_unknown") == "wxid_unknown"

    def test_private_chat_uses_contact_name(self, env):
        """私聊（非群）不走 group_cards，直接联系人昵称。"""
        env.svc.names["wxid_c"] = "王五"
        assert resolve_sender_display(
            env.db, env.svc.get_display_name, "wxid_c", "wxid_c") == "王五"

    def test_card_cache_hits_db_once(self, env):
        """群名片查询有进程内缓存：连续解析只查一次库。"""
        env.db.save_group_card(GROUP, "wxid_a", "张三")
        calls: list[str] = []
        original = env.db.get_group_cards

        def counting(chatroom: str) -> dict:
            calls.append(chatroom)
            return original(chatroom)

        env.db.get_group_cards = counting  # type: ignore[assignment]
        for _ in range(5):
            resolve_sender_display(env.db, env.svc.get_display_name,
                                   GROUP, "wxid_a")
        assert calls == [GROUP]

    def test_empty_wxid_returns_empty(self, env):
        assert resolve_sender_display(env.db, env.svc.get_display_name,
                                      GROUP, "") == ""

    def test_name_resolver_error_does_not_raise(self, env):
        env.svc.display_error.add("wxid_x")
        assert resolve_sender_display(
            env.db, env.svc.get_display_name, "wxid_x", "wxid_x") == "wxid_x"


class TestAnalysisLogSenderApi:
    def _insert(self, env, sender_wxid: str, chatroom: str = GROUP,
                text: str = "我要前保险杠") -> int:
        return env.db.insert_analysis_log(
            session_username=chatroom, sender_wxid=sender_wxid,
            msg_count=1, input_text=text, status="ok")

    def test_sender_display_in_list(self, env):
        env.db.save_group_card(GROUP, "wxid_a", "张三")
        self._insert(env, "wxid_a")
        self._insert(env, "wxid_b", chatroom="wxid_b")
        body = env.client.get("/api/analysis-logs").json()
        by_wxid = {i["sender_wxid"]: i for i in body["items"]}
        assert by_wxid["wxid_a"]["sender_display"] == "张三"
        # 私聊无昵称 → 回退 wxid
        assert by_wxid["wxid_b"]["sender_display"] == "wxid_b"

    def test_filter_by_group_card_name(self, env):
        """按群昵称检索：显示名解析成 wxid 候选后命中。"""
        env.db.save_group_card(GROUP, "wxid_a", "张三")
        self._insert(env, "wxid_a", text="我要前保险杠")
        self._insert(env, "wxid_b", text="我要大灯")
        body = env.client.get("/api/analysis-logs",
                              params={"sender": "张三"}).json()
        assert body["total"] == 1
        assert body["items"][0]["sender_wxid"] == "wxid_a"

    def test_filter_by_contact_name(self, env):
        """联系人昵称（非群名片）同样能命中（私聊场景）。"""
        env.svc.names["wxid_li"] = "李四"
        self._insert(env, "wxid_li", chatroom="wxid_li")
        self._insert(env, "wxid_a")
        body = env.client.get("/api/analysis-logs",
                              params={"sender": "李四"}).json()
        assert body["total"] == 1
        assert body["items"][0]["sender_wxid"] == "wxid_li"

    def test_filter_by_wxid_fragment(self, env):
        """贴 wxid 片段仍走原 LIKE 路径命中。"""
        self._insert(env, "wxid_zhang")
        self._insert(env, "wxid_li")
        body = env.client.get("/api/analysis-logs",
                              params={"sender": "zhang"}).json()
        assert body["total"] == 1
        assert body["items"][0]["sender_wxid"] == "wxid_zhang"

    def test_filter_no_match_returns_zero(self, env):
        self._insert(env, "wxid_a")
        body = env.client.get("/api/analysis-logs",
                              params={"sender": "查无此人"}).json()
        assert body["total"] == 0

    def test_filter_case_insensitive(self, env):
        env.db.save_group_card(GROUP, "wxid_a", "Alex")
        self._insert(env, "wxid_a")
        body = env.client.get("/api/analysis-logs",
                              params={"sender": "alex"}).json()
        assert body["total"] == 1

    def test_sender_with_status_combined(self, env):
        env.db.save_group_card(GROUP, "wxid_a", "张三")
        self._insert(env, "wxid_a")
        env.db.insert_analysis_log(session_username=GROUP, sender_wxid="wxid_a",
                                   msg_count=1, input_text="闲聊",
                                   status="skipped", error="预筛未命中")
        body = env.client.get("/api/analysis-logs", params={
            "sender": "张三", "status": "skipped"}).json()
        assert body["total"] == 1

    def test_display_error_does_not_500(self, env):
        """单条发送人解析失败只跳过该条 / 回退 wxid，不能整页 500。"""
        env.svc.display_error.add("wxid_a")
        self._insert(env, "wxid_a")
        resp = env.client.get("/api/analysis-logs")
        assert resp.status_code == 200
        assert resp.json()["items"][0]["sender_display"] == "wxid_a"

    def test_empty_sender_returns_all(self, env):
        self._insert(env, "wxid_a")
        self._insert(env, "wxid_b")
        assert env.client.get("/api/analysis-logs").json()["total"] == 2


class TestDistinctLogSenders:
    def test_distinct_senders(self):
        db = Database(":memory:")
        db.insert_analysis_log(session_username=GROUP, sender_wxid="wxid_a",
                               msg_count=1, input_text="1", status="ok")
        db.insert_analysis_log(session_username=GROUP, sender_wxid="wxid_a",
                               msg_count=1, input_text="2", status="ok")
        db.insert_analysis_log(session_username="wxid_b",
                               sender_wxid="wxid_b",
                               msg_count=1, input_text="3", status="ok")
        db.insert_analysis_log(session_username=GROUP, sender_wxid="",
                               msg_count=1, input_text="4", status="ok")
        senders = db.distinct_log_senders()
        assert sorted(senders) == ["wxid_a", "wxid_b"]
        # 最近出现优先（id 倒序）
        assert senders[0] == "wxid_b"
        pairs = db.distinct_log_sender_pairs()
        assert (GROUP, "wxid_a") in [(p[1], p[0]) for p in pairs]
        db.close()

    def test_limit_cap(self):
        db = Database(":memory:")
        for i in range(5):
            db.insert_analysis_log(session_username=GROUP,
                                   sender_wxid=f"wxid_{i}",
                                   msg_count=1, input_text="x", status="ok")
        assert len(db.distinct_log_senders(limit=2)) == 2
        db.close()


# ---------------------------------------------------------------------------
# 需求 1：管线把 raw_refs 落库（新建 + 合并两条路径）
# ---------------------------------------------------------------------------
HIT = {"hit": True, "type": "汽车配件", "points": ["有前保险杠现货"],
       "confidence": 88,
       "quote": "[图片] 识别：车辆/汽车配件（前保险杠）", "direction": "sell"}


def build_pipeline(result: dict):
    """构造走真实落库逻辑的管线（AI 客户端为替身）。"""
    db = Database(":memory:")
    db.insert_type("汽车配件", "汽车配件 OE 号 / 车架号 / 配件实物照片")
    db.set_setting("ai.base_url", "https://mock.local/v1")
    db.set_setting("ai.model", "mock-model")
    db.set_setting("compliance.confirmed", "1")
    client = FakeChatClient(result=dict(result))
    return db, Pipeline(db, Notifier(), client_factory=lambda cfg: client), client


def make_now_unit(*, lines, local_ids, kinds) -> AnalysisUnit:
    """窗口时间取「现在」，保证能落进商机合并窗口（用于合并路径测试）。"""
    now = datetime.now().isoformat(timespec="seconds")
    return AnalysisUnit(
        session_username=GROUP, session_name="配件群", session_kind="group",
        sender_wxid="wxid_a", sender_name="张三",
        window_start=now, window_end=now, merged_count=len(lines),
        lines=list(lines), local_ids=list(local_ids), line_kinds=list(kinds),
        has_image_sell=True,
    )


class TestPipelineRawRefsWiring:
    @pytest.fixture(autouse=True)
    def _fake_key(self, monkeypatch):
        monkeypatch.setattr("app.pipeline.decrypt_api_key", lambda raw: "sk-test")

    def test_new_opportunity_has_raw_refs(self):
        db, pipeline, _client = build_pipeline(HIT)
        unit = make_now_unit(
            lines=[("20:32", "我要前保险杠"),
                   ("20:32", "[图片] 识别：车辆/汽车配件（前保险杠）")],
            local_ids=[130, 131], kinds=["text", "image"])
        pipeline._process(unit)
        _total, rows = db.list_opportunities()
        assert len(rows) == 1
        refs = json.loads(db.get_opportunity(rows[0]["id"])["raw_refs"])
        assert refs == [{
            "line": "[20:32] [图片] 识别：车辆/汽车配件（前保险杠）",
            "local_id": 131, "kind": "image"}]
        db.close()

    def test_merge_appends_raw_refs(self):
        """同一人窗口内第二批图片 → 合并，raw_refs 数组追加而不是覆盖。"""
        db, pipeline, client = build_pipeline(HIT)
        pipeline._process(make_now_unit(
            lines=[("20:32", "[图片] 识别：车辆/汽车配件（前保险杠）")],
            local_ids=[131], kinds=["image"]))
        client.result = dict(HIT, points=["还有大灯现货"],
                             quote="[图片] 识别：车辆/汽车配件（大灯）")
        pipeline._process(make_now_unit(
            lines=[("20:33", "[图片] 识别：车辆/汽车配件（大灯）")],
            local_ids=[132], kinds=["image"]))
        _total, rows = db.list_opportunities()
        assert len(rows) == 1                     # 合并成一条
        refs = json.loads(db.get_opportunity(rows[0]["id"])["raw_refs"])
        assert [r["local_id"] for r in refs] == [131, 132]
        assert refs[1]["line"] == "[20:33] [图片] 识别：车辆/汽车配件（大灯）"
        db.close()

    def test_text_only_unit_writes_empty_refs(self):
        db, pipeline, _client = build_pipeline(HIT)
        unit = make_now_unit(lines=[("20:32", "我要前保险杠")],
                             local_ids=[130], kinds=["text"])
        unit.has_image_sell = False
        pipeline._process(unit)
        _total, rows = db.list_opportunities()
        assert db.get_opportunity(rows[0]["id"])["raw_refs"] == "[]"
        db.close()


# ---------------------------------------------------------------------------
# 需求 3：启动端口可配置
# ---------------------------------------------------------------------------
class TestRunPortConfig:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("KEFU_PORT", raising=False)
        monkeypatch.delenv("KEFU_HOST", raising=False)
        assert run_module.resolve_address([]) == ("127.0.0.1", 8765)

    def test_env_overrides_default(self, monkeypatch):
        monkeypatch.setenv("KEFU_PORT", "9001")
        monkeypatch.setenv("KEFU_HOST", "0.0.0.0")
        assert run_module.resolve_address([]) == ("0.0.0.0", 9001)

    def test_cli_overrides_env(self, monkeypatch):
        monkeypatch.setenv("KEFU_PORT", "9001")
        monkeypatch.setenv("KEFU_HOST", "0.0.0.0")
        assert run_module.resolve_address(
            ["--host", "127.0.0.1", "--port", "9100"]) == ("127.0.0.1", 9100)

    @pytest.mark.parametrize("bad", ["0", "65536", "-1", "abc", "80.5"])
    def test_invalid_cli_port_exit_2(self, bad, monkeypatch):
        monkeypatch.delenv("KEFU_PORT", raising=False)
        with pytest.raises(SystemExit) as exc:
            run_module.resolve_address(["--port", bad])
        assert exc.value.code == 2

    def test_invalid_env_port_exit_2(self, monkeypatch):
        monkeypatch.setenv("KEFU_PORT", "70000")
        with pytest.raises(SystemExit) as exc:
            run_module.resolve_address([])
        assert exc.value.code == 2

    def test_empty_env_port_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("KEFU_PORT", "   ")
        assert run_module.resolve_address([]) == ("127.0.0.1", 8765)
