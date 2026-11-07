# -*- coding: utf-8 -*-
"""QA 独立验证测试（优化 1 商机合并 + 优化 2 监控配额）。

聚焦团队交办的四个边界/并发疑点：
1. 合并后 window_end 续命语义定性（链条式合并是否会无限延长一条商机）
2. 双 worker 并发合并的丢失更新竞态（find_mergeable + merge_opportunity 非原子）
3. find 之后商机被回复/忽略、merge 仍覆写的陈旧快照竞态
4. POST /api/monitor-targets 配额 check-then-insert 的并发越界

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_qa_verify.py -v

历史说明：BUG-1/BUG-2/BUG-3 修复前曾以 xfail(strict=True) 标记；
工程师已全部修复，现转为常规断言（回归修复行为）。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import List

import pytest

from app.aggregator import AnalysisUnit, Aggregator
from app.config import MAX_MONITOR_TARGETS
from app.crypto import encrypt_api_key
from app.db import Database
from app.notifier import Notifier
from app.pipeline import Pipeline
from tests.test_core import FakeChatClient, make_msg, recent_unit  # noqa: F401


# ---------------------------------------------------------------------------
# 疑点 1：window_end 续命定性（链条式合并）
# ---------------------------------------------------------------------------
class TestWindowEndRenewalSemantics:
    """定性：只要相邻两批命中间隔 < 商机合并窗口，同一商机将持续续命。"""

    def _prepare(self):
        db = Database(":memory:")
        notifier = Notifier()
        db.insert_type("机械加工订单", "desc")
        db.set_setting("ai.base_url", "https://mock.local/v1")
        db.set_setting("ai.api_key_enc", encrypt_api_key("sk-test-12345678"))
        db.set_setting("ai.model", "mock-model")
        db.set_setting("compliance.confirmed", "1")
        return db, notifier

    def test_chain_merge_renews_window_end(self):
        """三批命中各距上一批 <180s → 合并为一条，window_end 随每批顺延。

        结论：合并窗口是「滚动窗口」——window_end 每次合并被更新为新批次
        末时间，因此持续发言（每批间隔 <180s）会让同一条商机无限续命。
        这符合「距最后一批命中 180s 内合并」的字面语义（连续发言不碎片化），
        但 raw_text / merged_count 无上限，长期会话会无限膨胀（见报告）。
        """
        db, notifier = self._prepare()
        client = FakeChatClient(result={"hit": True, "type": "机械加工订单",
                                        "points": ["p"], "confidence": 85,
                                        "quote": "q"})
        pipeline = Pipeline(db, notifier, client_factory=lambda cfg: client)
        pipeline._process(recent_unit([("13:00", "第一批")],
                                      window_end_offset_sec=-170))
        pipeline._process(recent_unit([("13:02", "第二批")],
                                      window_end_offset_sec=-100))
        pipeline._process(recent_unit([("13:04", "第三批")],
                                      window_end_offset_sec=-10))
        total, rows = db.list_opportunities()
        assert total == 1                       # 链条式合并：始终一条
        opp = db.get_opportunity(rows[0]["id"])
        assert opp["merged_count"] == 3
        # window_end 已续命到最后一批（允许跨秒边界 ±1s）
        end_epoch = datetime.fromisoformat(opp["window_end"]).timestamp()
        assert abs(end_epoch - (time.time() - 10)) <= 2.0
        assert "第三批" in opp["raw_text"]


# ---------------------------------------------------------------------------
# 疑点 2：双 worker 并发合并的丢失更新竞态
# ---------------------------------------------------------------------------
class TestConcurrentMergeRace:
    def _prepare(self):
        db = Database(":memory:")
        notifier = Notifier()
        db.insert_type("机械加工订单", "desc")
        db.set_setting("ai.base_url", "https://mock.local/v1")
        db.set_setting("ai.api_key_enc", encrypt_api_key("sk-test-12345678"))
        db.set_setting("ai.model", "mock-model")
        db.set_setting("compliance.confirmed", "1")
        return db, notifier

    def test_two_workers_merge_same_opportunity_no_lost_update(self):
        """两个 worker 同时把同发送人的两批命中合并进同一条商机。

        Round 2 回归（BUG-1 已修复：merge_opportunity 单条原子 UPDATE，
        raw_text SQL 端拼接、merged_count SQL 端累加）：
        期望 merged_count = 1+1+1 = 3，三批文本都在 raw_text 中。
        用 Barrier 在 find_mergeable_opportunity 处强制两个 worker 都先
        拿到陈旧快照再放行，保证并发窗口被确定性覆盖。
        """
        db, notifier = self._prepare()
        client = FakeChatClient(result={"hit": True, "type": "机械加工订单",
                                        "points": ["p"], "confidence": 85,
                                        "quote": "q"})
        pipeline = Pipeline(db, notifier, worker_count=2,
                            client_factory=lambda cfg: client)
        # 先串行产生第一条商机（merged_count=1，window_end 最近）
        pipeline._process(recent_unit([("14:00", "第一批")],
                                      window_end_offset_sec=-5))
        _, rows = db.list_opportunities()
        first_id = rows[0]["id"]

        # 在 find_mergeable 处用 Barrier 强制两个 worker 都先拿到陈旧快照
        # 再继续（先各自 find → 齐了才返回），确定性覆盖竞态窗口
        barrier = threading.Barrier(2, timeout=15)
        orig_find = db.find_mergeable_opportunity

        def synced_find(**kwargs):
            snapshot = orig_find(**kwargs)   # 先拿到快照
            barrier.wait()                   # 双方都拿到后才放行
            return snapshot

        db.find_mergeable_opportunity = synced_find  # type: ignore[method-assign]

        pipeline.start()
        try:
            pipeline.enqueue(recent_unit([("14:05", "第二批")],
                                         window_end_offset_sec=-3))
            pipeline.enqueue(recent_unit([("14:08", "第三批")],
                                         window_end_offset_sec=-1))
            deadline = time.time() + 15
            while time.time() < deadline:
                opp = db.get_opportunity(first_id)
                if opp and opp["merged_count"] >= 3:
                    break
                time.sleep(0.05)
        finally:
            pipeline.stop()
            db.find_mergeable_opportunity = orig_find  # type: ignore[method-assign]

        total, rows = db.list_opportunities()
        assert total == 1                       # 都合并进了同一条（无新建）
        opp = db.get_opportunity(first_id)
        assert opp["merged_count"] == 3         # SQL 端累加：三批全计入
        raw = opp["raw_text"]
        assert "第一批" in raw and "第二批" in raw and "第三批" in raw


# ---------------------------------------------------------------------------
# 疑点 3：find 之后商机被回复，merge 仍覆写（陈旧快照的另一表现）
# ---------------------------------------------------------------------------
class TestMergeAfterStatusChange:
    def _prepare(self):
        db = Database(":memory:")
        notifier = Notifier()
        db.insert_type("机械加工订单", "desc")
        db.set_setting("ai.base_url", "https://mock.local/v1")
        db.set_setting("ai.api_key_enc", encrypt_api_key("sk-test-12345678"))
        db.set_setting("ai.model", "mock-model")
        db.set_setting("compliance.confirmed", "1")
        return db, notifier

    def test_merge_into_replied_opportunity_rejected(self):
        """Round 2 回归（BUG-2 已修复：merge_opportunity 带
        WHERE status='pending' 复检）：find 返回 pending 商机 → 用户回复 →
        merge 被拒（rowcount=0 返回 None）→ pipeline 走新建兜底。

        断言：已回复商机的原文/窗口不被覆写；新批次生成新 pending 商机
        （内容不被吞）并广播 opportunity_new。
        """
        db, notifier = self._prepare()
        client = FakeChatClient(result={"hit": True, "type": "机械加工订单",
                                        "points": ["新要点"], "confidence": 85,
                                        "quote": "新引文"})
        pipeline = Pipeline(db, notifier, client_factory=lambda cfg: client)
        pipeline._process(recent_unit([("14:00", "第一批")],
                                      window_end_offset_sec=-5))
        _, rows = db.list_opportunities()
        opp_id = rows[0]["id"]
        before = db.get_opportunity(opp_id)

        # 模拟 find 与 merge 之间用户点击了回复
        unit = recent_unit([("14:05", "第二批")], window_end_offset_sec=-1)
        analysis_cfg = {"opp_merge_window_sec": 180}
        snapshot = pipeline._find_mergeable(analysis_cfg, unit)
        assert snapshot is not None and snapshot["id"] == opp_id
        db.update_opportunity_status(opp_id, "replied")

        events: List[str] = []
        notifier.broadcast = lambda event, data: events.append(event)
        result = {"hit": True, "type": "机械加工订单", "points": ["新要点"],
                  "confidence": 85, "quote": "新引文"}
        pipeline._merge_into(snapshot, unit, result, None, "机械加工订单",
                             {}, "mock", 10)
        # 已回复商机不被覆写
        after = db.get_opportunity(opp_id)
        assert after["raw_text"] == before["raw_text"]
        assert after["window_end"] == before["window_end"]
        assert after["status"] == "replied"
        # 新批次走新建兜底：内容不被吞
        assert events.count("opportunity_new") == 1
        total, rows = db.list_opportunities()
        assert total == 2
        new_opp = db.get_opportunity(max(r["id"] for r in rows))
        assert new_opp["status"] == "pending"
        assert "第二批" in new_opp["raw_text"]


# ---------------------------------------------------------------------------
# 疑点 4：监控配额 check-then-insert 并发越界
# ---------------------------------------------------------------------------
class TestMonitorQuotaConcurrency:
    def test_concurrent_add_at_last_quota_slot(self):
        """Round 2 回归（BUG-3 已修复：db.insert_target_within_limit 在
        同一把写锁内完成「计数 + INSERT」）：199/200 时两请求并发添加，
        必须恰好 1 个成功、1 个 400，总数恰为上限 200。"""
        from app.api.router_targets import create_target
        from app.schemas import MonitorTargetCreate

        db = Database(":memory:")
        for i in range(MAX_MONITOR_TARGETS - 1):   # 199 个
            db.insert_target(f"wxid_full_{i}", f"联系人{i}", "friend")

        barrier = threading.Barrier(2, timeout=15)

        class SyncSvc:
            """在 get_display_name（配额校验之前）同步两请求，
            并补齐 create_target 需要的监听联动空实现。"""

            def get_display_name(self, username: str) -> str:
                barrier.wait()
                return username

            def register_target(self, username: str) -> None:
                pass

            def unregister_target(self, username: str) -> None:
                pass

        results: List[object] = []
        errors: List[BaseException] = []

        def worker(i: int) -> None:
            try:
                results.append(create_target(
                    MonitorTargetCreate(username=f"wxid_new_{i}"),
                    db=db, svc=SyncSvc()))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        # 恰好一胜一负：胜者 201 语义返回 target，负者 HTTPException(400)
        assert len(results) == 1
        assert len(errors) == 1
        rejected = errors[0]
        from fastapi import HTTPException
        assert isinstance(rejected, HTTPException)
        assert rejected.status_code == 400
        assert "上限" in str(rejected.detail)
        # 总数恰好达到上限，不越界
        assert db.count_targets() == MAX_MONITOR_TARGETS


# ---------------------------------------------------------------------------
# 补充定性：aggregator 静默窗口 + 合并窗口组合时序
# ---------------------------------------------------------------------------
class TestMicroWindowAndMergeWindowCombo:
    def test_continuous_speech_within_window_never_flushes(self):
        """定性（预先存在的设计属性）：持续发言（相邻消息间隔 < 微窗口）
        永不触发 flush → 分析延迟无上界、缓冲无上限。
        微窗口默认从 300s 降到 60s 缓解但未消除该属性。
        """
        flushed: List[AnalysisUnit] = []
        agg = Aggregator(flush_callback=flushed.append,
                         window_sec_provider=lambda: 60)
        now = time.time()
        agg.add(make_msg("第一条", 1, create_time=now), "群", "group")
        agg.add(make_msg("第二条", 2, create_time=now + 30), "群", "group")
        agg.add(make_msg("第三条", 3, create_time=now + 55), "群", "group")
        agg._flush_expired()      # 静默期从未达到 60s
        assert flushed == []      # 仍在缓冲：商机分析被持续顺延

    def test_continuous_speech_over_force_flush_valve_gets_flushed(self):
        """Round 2 回归（O-2 安全阀）：持续发言（相邻间隔恒 < 微窗口）本会
        永不 flush，但首条消息年龄达到 微窗口×10 后被无条件强制 flush，
        且整个缓冲（全部消息）一次性带出——分析延迟与缓冲大小从此有上界。
        """
        from app.config import AGGREGATOR_FORCE_FLUSH_FACTOR
        flushed: List[AnalysisUnit] = []
        agg = Aggregator(flush_callback=flushed.append,
                         window_sec_provider=lambda: 5)
        now = time.time()
        # 持续发言序列：间隔 4s（< 微窗口 5s，静默条件永不满足），
        # 首条年龄 52s > 5×10=50（强制 flush 阈值）
        for i in range(5):
            agg.add(make_msg(f"连续第{i + 1}句", i + 1,
                             create_time=now - 52 + i * 4), "群", "group")
        agg._flush_expired()
        assert len(flushed) == 1                  # 安全阀触发，整缓冲带出
        unit = flushed[0]
        assert unit.merged_count == 5
        for i in range(1, 6):
            assert f"连续第{i}句" in unit.merged_text()
        assert AGGREGATOR_FORCE_FLUSH_FACTOR == 10  # 契约：安全阀倍数

    def test_pause_over_micro_window_then_quick_batches_no_fragmentation(self):
        """停顿 >60s 后 flush，随后 180s 内的连续批次合并 → 不碎片化。"""
        db = Database(":memory:")
        notifier = Notifier()
        db.insert_type("机械加工订单", "desc")
        db.set_setting("ai.base_url", "https://mock.local/v1")
        db.set_setting("ai.api_key_enc", encrypt_api_key("sk-test-12345678"))
        db.set_setting("ai.model", "mock-model")
        db.set_setting("compliance.confirmed", "1")
        client = FakeChatClient(result={"hit": True, "type": "机械加工订单",
                                        "points": ["p"], "confidence": 85,
                                        "quote": "q"})
        pipeline = Pipeline(db, notifier, client_factory=lambda cfg: client)
        # 批 1（距今 400s，超过合并窗口）→ 新建；批 2/3 在窗口内 → 合并进批 2
        pipeline._process(recent_unit([("13:00", "很久前的一批")],
                                      window_end_offset_sec=-400))
        pipeline._process(recent_unit([("13:10", "新会话第一批")],
                                      window_end_offset_sec=-60))
        pipeline._process(recent_unit([("13:12", "新会话第二批")],
                                      window_end_offset_sec=-5))
        total, rows = db.list_opportunities()
        assert total == 2                       # 未碎片化成 3 条
        newest = max(rows, key=lambda r: r["id"])
        assert db.get_opportunity(newest["id"])["merged_count"] == 2
