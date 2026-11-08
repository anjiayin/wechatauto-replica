# -*- coding: utf-8 -*-
"""QA 严过关独立验证测试（6 项用户反馈优化）。

覆盖（与验证任务清单一一对应）：
- 优化 1 会话列表缓存：TTL 过期重建、构建失败回退过期缓存、并发首请求
  单飞（single-flight，实现报告声称有「并发构建锁」，需证实/证伪）
- 优化 2 预筛中文逗号：re.split(r'[,，]') 中英文逗号均可分隔关键词
- 优化 4 详情/上下文拆分契约：详情响应不再含 context 字段、
  context 接口 404 分支
- 优化 5 消息查询重试：异常耗尽 → 「数据库正忙」；空结果耗尽 →
  按会话无消息处理（error 为空）；默认路径空结果不重试不延迟
- 优化 6 分析日志 + 修正示例：analysis-logs 非法参数 422；
  correction_examples None=不变更 / []=显式清空 / 21 条 FIFO 淘汰 /
  去空 + 截 200 字

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_qa_ux6.py -v
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import analyzer
from app.api import ALL_ROUTERS
from app.api import deps
from app.db import Database
from app.notifier import Notifier
from tests.test_api import FakePipeline, FakeSender, FakeWechatService, insert_opp
from tests.test_core import FakeWxDb, make_svc


# ---------------------------------------------------------------------------
# 夹具（与 test_api.env 同构：真实路由 + 替身依赖 + 临时库）
# ---------------------------------------------------------------------------
@pytest.fixture()
def env(tmp_path):
    db = Database(str(tmp_path / "qa_ux6.sqlite3"))
    svc = FakeWechatService()
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


def _analysis_body(**overrides) -> dict:
    """PUT /api/settings/analysis 的最小合法请求体。"""
    body = {
        "strictness": "balanced",
        "min_confidence": 60,
        "prescreen_enabled": False,
        "prescreen_keywords": "",
        "prescreen_regex": "",
        "merge_window_sec": 15,
        "opp_merge_window_sec": 180,
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# 优化 1：会话列表缓存
# ---------------------------------------------------------------------------
class TestSessionCacheUx6:
    def test_ttl_expiry_rebuilds_and_recovers(self):
        """TTL 过期 → 重建；构建失败回退过期缓存后，恢复时重建成功。"""
        svc = make_svc()
        assert svc.search_sessions() != []
        wxdb = svc._wxdb
        assert wxdb.sessions_calls == 1
        # 未过期走缓存
        svc.search_sessions(keyword="王五")
        assert wxdb.sessions_calls == 1
        # 过期 → 重建
        svc._contact_cache_ts -= 61
        svc.search_sessions()
        assert wxdb.sessions_calls == 2
        # 失败回退：构建抛异常 → 返回过期缓存、不抛错
        stale = list(svc._contact_cache)

        def boom() -> List[dict]:
            raise RuntimeError("微信库忙")

        svc._build_contact_list = boom  # type: ignore[method-assign]
        svc._contact_cache_ts -= 61
        assert svc.search_sessions() == stale
        # 恢复：去掉故障后能重建出正常列表
        del svc._build_contact_list  # type: ignore[attr-defined]
        del wxdb  # noqa: F841  （占位，保持下方断言清晰）
        svc._contact_cache_ts -= 61
        assert svc.search_sessions() != []

    def test_build_failure_fallback_does_not_pin_stale_ts(self):
        """失败回退不更新时间戳：下次请求会再次尝试重建（不会永久陈旧）。"""
        svc = make_svc()
        svc.search_sessions()
        ts_after_build = svc._contact_cache_ts

        def boom() -> List[dict]:
            raise RuntimeError("微信库忙")

        svc._build_contact_list = boom  # type: ignore[method-assign]
        svc._contact_cache_ts -= 61
        svc.search_sessions()  # 回退过期缓存
        assert svc._contact_cache_ts == ts_after_build - 61  # 未被刷新

    def test_concurrent_first_request_single_flight(self):
        """并发首请求只应构建一次（single-flight）。

        期望：N 个并发首请求共享一次构建（实现报告声称有「并发构建锁」）。
        前端设置页本身就会并行发 group/friend 两个搜索请求，若不单飞，
        冷缓存时会并发撞两次微信库冷分片（各 ~20s）。
        """
        svc = make_svc()
        orig_get_sessions = svc._wxdb.get_sessions

        def slow_get_sessions(limit: int = 100) -> list[dict]:
            time.sleep(0.3)  # 模拟微信库冷启动慢查询
            return orig_get_sessions(limit=limit)

        svc._wxdb.get_sessions = slow_get_sessions  # type: ignore[method-assign]
        results: List[list] = []

        def worker() -> None:
            results.append(svc.search_sessions())

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert len(results) == 2
        assert svc._wxdb.sessions_calls == 1, (
            f"并发首请求重复构建缓存（sessions_calls="
            f"{svc._wxdb.sessions_calls}），缺少并发构建锁/single-flight"
        )
        # 两次请求结果一致（同一份缓存数据）
        assert results[0] == results[1]


# ---------------------------------------------------------------------------
# 优化 2：预筛中文逗号
# ---------------------------------------------------------------------------
class TestPrescreenChineseComma:
    @pytest.mark.parametrize("keywords,text,expected", [
        # 中文逗号分隔的关键词应全部生效
        ("采购，报价", "有个报价需求，帮我看看", True),
        ("采购，报价", "今天天气不错", False),
        # 英文逗号（回归）
        ("采购,报价", "求购一批轴承", False),
        ("采购,报价", "想采购一批轴承", True),
        # 中英文混用
        ("采购,报价，加工", "接加工订单的联系我", True),
        # 关键词为空/纯逗号 → 不误伤（全部通过）
        ("，，", "任意内容", True),
        ("", "任意内容", True),
        # 关键词命中 + 正则共存
        ("采购，报价", "CNC 加工 500 件", False),
    ])
    def test_prescreen_keyword_split(self, keywords, text, expected):
        assert analyzer.prescreen(text, keywords, "") is expected


# ---------------------------------------------------------------------------
# 优化 4：详情/上下文拆分契约
# ---------------------------------------------------------------------------
class TestDetailContextContract:
    def test_detail_response_has_no_context_field(self, env):
        """契约变更：详情响应不再包含 context 字段（拆分为独立接口）。"""
        opp = insert_opp(env.db)
        resp = env.client.get(f"/api/opportunities/{opp['id']}")
        assert resp.status_code == 200
        body = resp.json()
        assert "context" not in body
        # 详情本身字段完整（业务库字段）
        assert body["raw_text"] == opp["raw_text"]
        assert body["id"] == opp["id"]

    def test_context_endpoint_404_for_missing(self, env):
        resp = env.client.get("/api/opportunities/99999/context")
        assert resp.status_code == 404
        assert "商机不存在" in resp.json()["detail"]

    def test_context_endpoint_shape(self, env):
        """context 接口返回 {items, error} 两字段。"""
        opp = insert_opp(env.db)
        resp = env.client.get(f"/api/opportunities/{opp['id']}/context")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"items", "error"}
        assert body["error"] == ""
        assert isinstance(body["items"], list)


# ---------------------------------------------------------------------------
# 优化 5：消息查询重试 —— 两种「空」的区分
# ---------------------------------------------------------------------------
class TestRetryTwoKindsOfEmpty:
    def test_exception_exhausted_returns_busy_error(self, monkeypatch):
        """异常重试耗尽 → 返回「数据库正忙」可读错误。"""
        monkeypatch.setattr("app.wechat_service.time.sleep", lambda s: None)
        svc = make_svc()
        svc._wxdb.msg_fail_times = 99
        msgs, error = svc.get_messages_with_error("111@chatroom",
                                                  retry_on_empty=True)
        assert msgs == []
        assert "微信数据库正忙" in error
        assert "重新加载" in error
        assert svc._wxdb.msg_calls == 3  # 1 次初始 + 2 次重试

    def test_empty_exhausted_treated_as_no_messages(self, monkeypatch):
        """空结果重试耗尽仍为空 → 按「会话确实无消息」处理（error 为空）。"""
        monkeypatch.setattr("app.wechat_service.time.sleep", lambda s: None)
        svc = make_svc()
        svc._wxdb.msgs = []
        msgs, error = svc.get_messages_with_error("empty@chatroom",
                                                  retry_on_empty=True)
        assert msgs == [] and error == ""
        assert svc._wxdb.msg_calls == 3  # 空结果也重试了

    def test_default_path_empty_no_retry(self, monkeypatch):
        """默认路径（消息浏览页）：空结果不重试、立即返回、不拖 3 秒。"""
        monkeypatch.setattr("app.wechat_service.time.sleep", lambda s: None)
        svc = make_svc()
        svc._wxdb.msgs = []
        msgs, error = svc.get_messages_with_error("empty@chatroom")
        assert msgs == [] and error == ""
        assert svc._wxdb.msg_calls == 1

    def test_retry_sleep_total_bounded(self, monkeypatch):
        """重试间隔累计有界：1.5s × 2 = 3s（不随失败次数无限放大）。"""
        sleeps: List[float] = []
        monkeypatch.setattr("app.wechat_service.time.sleep",
                            lambda s: sleeps.append(s))
        svc = make_svc()
        svc._wxdb.msg_fail_times = 99
        svc.get_messages_with_error("111@chatroom")
        assert len(sleeps) == 2
        assert all(s == 1.5 for s in sleeps)


# ---------------------------------------------------------------------------
# 优化 6：分析日志接口 + 修正示例
# ---------------------------------------------------------------------------
class TestAnalysisLogsApi:
    def test_insert_and_list(self, env):
        env.db.insert_analysis_log(session_username="111@chatroom",
                                   sender_wxid="wxid_a", msg_count=2,
                                   input_text="想采购一批轴承",
                                   result='{"hit": true}', status="ok")
        resp = env.client.get("/api/analysis-logs")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["session_username"] == "111@chatroom"

    @pytest.mark.parametrize("query", [
        "?page=0",                       # page 从 1 起
        "?page=-1",
        "?page_size=0",
        "?page_size=101",                # 上限 100
        "?status=ok1",                   # 非法状态
        "?status=OK",                    # 大小写敏感
        "?page=abc",
    ])
    def test_invalid_params_422(self, env, query):
        resp = env.client.get(f"/api/analysis-logs{query}")
        assert resp.status_code == 422

    def test_filters(self, env):
        env.db.insert_analysis_log(session_username="111@chatroom",
                                   msg_count=1, input_text="求购轴承",
                                   status="ok")
        env.db.insert_analysis_log(session_username="wxid_b",
                                   msg_count=1, input_text="求购齿轮",
                                   status="skipped", error="预筛未命中")
        # session 模糊筛选
        body = env.client.get("/api/analysis-logs?session=111").json()
        assert body["total"] == 1
        assert body["items"][0]["session_username"] == "111@chatroom"
        # keyword 筛选
        body = env.client.get("/api/analysis-logs?keyword=齿轮").json()
        assert body["total"] == 1
        assert body["items"][0]["status"] == "skipped"
        # status 筛选
        body = env.client.get("/api/analysis-logs?status=skipped").json()
        assert body["total"] == 1
        # 分页
        body = env.client.get("/api/analysis-logs?page=1&page_size=1").json()
        assert body["total"] == 2 and len(body["items"]) == 1
        body = env.client.get("/api/analysis-logs?page=2&page_size=1").json()
        assert body["total"] == 2 and len(body["items"]) == 1


class TestCorrectionExamplesApi:
    def test_put_none_keeps_existing(self, env):
        """correction_examples 缺省（None）= 不变更，防部分更新误清空。"""
        env.client.put("/api/settings/analysis",
                       json=_analysis_body(correction_examples=["示例一"]))
        resp = env.client.put("/api/settings/analysis",
                              json=_analysis_body())  # 不带 correction_examples
        assert resp.status_code == 200
        assert resp.json()["correction_examples"] == ["示例一"]

    def test_put_empty_list_clears(self, env):
        """[]=显式清空（与 None 区分）。"""
        env.client.put("/api/settings/analysis",
                       json=_analysis_body(correction_examples=["示例一"]))
        resp = env.client.put("/api/settings/analysis",
                              json=_analysis_body(correction_examples=[]))
        assert resp.json()["correction_examples"] == []

    def test_put_21_items_fifo_drops_oldest(self, env):
        """21 条 → 保留最新 20 条，淘汰最旧 1 条。"""
        items = [f"示例{i:02d}" for i in range(1, 22)]
        resp = env.client.put("/api/settings/analysis",
                              json=_analysis_body(correction_examples=items))
        kept = resp.json()["correction_examples"]
        assert len(kept) == 20
        assert kept[0] == "示例02"   # 最旧的 示例01 被淘汰
        assert kept[-1] == "示例21"

    def test_put_exactly_20_no_drop(self, env):
        items = [f"示例{i:02d}" for i in range(1, 21)]
        resp = env.client.put("/api/settings/analysis",
                              json=_analysis_body(correction_examples=items))
        kept = resp.json()["correction_examples"]
        assert len(kept) == 20
        assert kept[0] == "示例01"

    def test_put_strips_empty_and_truncates_200(self, env):
        long_text = "x" * 300
        resp = env.client.put(
            "/api/settings/analysis",
            json=_analysis_body(correction_examples=["  ", "", long_text]),
        )
        kept = resp.json()["correction_examples"]
        assert len(kept) == 1           # 空白项被去除
        assert len(kept[0]) == 200      # 截断到 200 字

    def test_get_returns_examples(self, env):
        env.client.put("/api/settings/analysis",
                       json=_analysis_body(correction_examples=["甲", "乙"]))
        body = env.client.get("/api/settings/analysis").json()
        assert body["correction_examples"] == ["甲", "乙"]

    def test_read_corrupt_setting_falls_back_empty(self, env):
        """settings 脏数据（非法 JSON / 非数组）→ 回退空列表，不崩接口。"""
        from app.db import read_correction_examples
        env.db.set_setting("analysis.correction_examples", "{not-json")
        assert read_correction_examples(env.db) == []
        env.db.set_setting("analysis.correction_examples", '{"a": 1}')
        assert read_correction_examples(env.db) == []
        resp = env.client.get("/api/settings/analysis")
        assert resp.status_code == 200
        assert resp.json()["correction_examples"] == []
