# -*- coding: utf-8 -*-
"""语音补转写队列（app/voice_retry.py）与链路接入测试。

被测行为（对应实测现象：连发两条语音，第二条报「未找到语音数据」）：

- 微信语音是「消息先到、音频后到」，首次转写撞上音频未落库属正常；
- 该失败**不能当终态丢弃**：按退避序列自动重试，补上后回填日志并重新
  纳入一次分析（否则这条语音的商机就永久漏了）；
- 配置/环境类失败（开关关闭、模型未装、库不可用）不重试，直接落终态；
- 重试用尽的终态文案要给出可操作的手动恢复路径；
- 手动重试（分析日志「重新识别」）走同一条链路。

测试策略：队列用极短退避（0.02s）+ 短轮询等待，不依赖真实微信库与模型。

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_voice_retry.py -q
"""
from __future__ import annotations

import threading
import time
from typing import List, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.aggregator import Aggregator, AnalysisUnit
from app.api import deps
from app.api.router_system import router as system_router
from app.db import Database
from app.notifier import Notifier
from app.voice_retry import MAX_PENDING, VoiceRetryQueue
from app.wechat_service import (
    MSG_ASR_DISABLED,
    MSG_ASR_NO_VOICE,
    WeChatService,
)

FAST_DELAYS = (0.02, 0.02, 0.02)


def wait_until(pred, timeout: float = 3.0) -> bool:
    """轮询等待条件成立（避免固定 sleep 造成的偶发失败）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def make_voice_msg(local_id: int = 114, username: str = "filehelper") -> dict:
    """构造一条语音消息回调 dict。"""
    return {
        "local_id": local_id,
        "type": "语音",
        "sender_id": 2,
        "sender_username": "",
        "create_time": time.time(),
        "content": '<msg><voicemsg length="3000"/></msg>',
        "sort_seq": local_id,
        "username": username,
    }


class Recorder:
    """回调记录器：把 on_success / on_failure 的入参存下来。"""

    def __init__(self) -> None:
        self.success: List[Tuple] = []
        self.failure: List[Tuple] = []
        self.lock = threading.Lock()

    def on_success(self, username, local_id, text, payload, attempt):
        with self.lock:
            self.success.append((username, local_id, text, payload, attempt))

    def on_failure(self, username, local_id, error, payload, attempts):
        with self.lock:
            self.failure.append((username, local_id, error, payload, attempts))


def make_queue(transcribe, rec: Recorder,
               delays: Tuple[float, ...] = FAST_DELAYS) -> VoiceRetryQueue:
    return VoiceRetryQueue(transcribe=transcribe, on_success=rec.on_success,
                           on_failure=rec.on_failure, delays=delays)


class TestQueue:
    def test_success_after_retry_calls_back(self):
        """前两次「取不到音频」、第三次成功 → on_success 收到正确 attempt。"""
        calls = {"n": 0}

        def transcribe(username, local_id):
            calls["n"] += 1
            return ("那家有机油滤芯", "") if calls["n"] >= 3 else (
                "", MSG_ASR_NO_VOICE)

        rec = Recorder()
        q = make_queue(transcribe, rec)
        q.start()
        try:
            assert q.enqueue("filehelper", 114, {"log_id": 7}) is True
            assert wait_until(lambda: len(rec.success) == 1)
        finally:
            q.stop()
        assert calls["n"] == 3
        username, local_id, text, payload, attempt = rec.success[0]
        assert (username, local_id, text) == ("filehelper", 114, "那家有机油滤芯")
        assert payload == {"log_id": 7}
        assert attempt == 3
        assert q.status()["succeeded"] == 1

    def test_exhausted_calls_failure(self):
        """一直取不到 → 尝试满 len(delays) 次后回调 on_failure。"""
        calls = {"n": 0}

        def transcribe(username, local_id):
            calls["n"] += 1
            return "", MSG_ASR_NO_VOICE

        rec = Recorder()
        q = make_queue(transcribe, rec)
        q.start()
        try:
            q.enqueue("filehelper", 114, {"log_id": 1})
            assert wait_until(lambda: len(rec.failure) == 1)
        finally:
            q.stop()
        assert calls["n"] == len(FAST_DELAYS)
        assert rec.failure[0][2] == MSG_ASR_NO_VOICE
        assert rec.failure[0][4] == len(FAST_DELAYS)
        assert rec.success == []
        assert q.status()["exhausted"] == 1

    def test_duplicate_enqueue_is_ignored(self):
        """同一 (会话, local_id) 在途时重复入队返回 False（不重复占额度）。"""
        rec = Recorder()
        q = make_queue(lambda u, l: ("", MSG_ASR_NO_VOICE), rec,
                       delays=(5.0,))
        q.start()
        try:
            assert q.enqueue("filehelper", 114, {}) is True
            assert q.enqueue("filehelper", 114, {}) is False
            assert q.enqueue("filehelper", 115, {}) is True
            assert q.pending_count() == 2
        finally:
            q.stop()

    def test_enqueue_rejects_invalid_args(self):
        rec = Recorder()
        q = make_queue(lambda u, l: ("", ""), rec)
        assert q.enqueue("", 1, {}) is False
        assert q.enqueue("a", 0, {}) is False
        assert q.enqueue("a", "不是数字", {}) is False

    def test_callback_exception_does_not_kill_worker(self):
        """回调抛异常必须被吞掉，后续任务照常处理。"""
        rec = Recorder()

        def on_success(username, local_id, text, payload, attempt):
            rec.on_success(username, local_id, text, payload, attempt)
            raise RuntimeError("回调炸了")

        q = VoiceRetryQueue(transcribe=lambda u, l: ("文本", ""),
                            on_success=on_success, on_failure=rec.on_failure,
                            delays=(0.02,))
        q.start()
        try:
            q.enqueue("filehelper", 1, {})
            q.enqueue("filehelper", 2, {})
            assert wait_until(lambda: len(rec.success) == 2)
        finally:
            q.stop()

    def test_transcribe_exception_counts_as_failure(self):
        """转写函数意外抛异常也当失败处理（不会让任务凭空消失）。"""
        def boom(username, local_id):
            raise RuntimeError("解密超时")

        rec = Recorder()
        q = make_queue(boom, rec, delays=(0.02,))
        q.start()
        try:
            q.enqueue("filehelper", 1, {})
            assert wait_until(lambda: len(rec.failure) == 1)
        finally:
            q.stop()
        assert "解密超时" in rec.failure[0][2]

    def test_stop_joins_thread(self):
        """stop 后工作线程退出，不会再处理队列。"""
        rec = Recorder()
        q = make_queue(lambda u, l: ("", MSG_ASR_NO_VOICE), rec, delays=(30.0,))
        q.start()
        q.enqueue("filehelper", 1, {})
        q.stop()
        assert q._thread is None
        assert rec.failure == []       # 30 秒后才尝试，stop 已放弃

    def test_stop_during_attempt_does_not_mark_exhausted(self):
        """服务停止打断在途任务 ≠ 重试用尽：不得落终态、不得误报失败。

        场景：worker 正在做某次尝试时用户重启服务，这次尝试被打断。
        若当成「已重试 N 次仍未取到」，用户重启一次就会把正常语音误标失败。
        """
        release = threading.Event()
        entered = threading.Event()

        def slow_transcribe(username, local_id):
            entered.set()
            release.wait(5)            # 等停止标志置位后再放行
            return "", MSG_ASR_NO_VOICE

        rec = Recorder()
        q = VoiceRetryQueue(transcribe=slow_transcribe,
                            on_success=rec.on_success,
                            on_failure=rec.on_failure,
                            delays=(0.05, 0.05))
        q.start()
        try:
            q.enqueue("filehelper", 1, {})
            assert entered.wait(3)     # worker 已进入第一次尝试
            # 直接置停止标志（等价于 stop() 的核心动作；不走 stop() 是因为
            # 它的 join 会阻塞在这次尚未返回的尝试上，测试等不起）
            q._stop.set()
        finally:
            release.set()
            q._thread and q._thread.join(timeout=5)
        assert rec.failure == []       # 不落「重试用尽」终态
        assert rec.success == []
        assert q.status()["exhausted"] == 0

    def test_status_snapshot(self):
        rec = Recorder()
        q = make_queue(lambda u, l: ("", MSG_ASR_NO_VOICE), rec,
                       delays=(30.0,))
        q.start()
        try:
            assert q.status()["pending"] == 0
            q.enqueue("filehelper", 1, {})
            status = q.status()
            assert status["pending"] == 1
            assert status["next_in_sec"] >= 0
            assert status["delays"] == [30.0]
            assert status["enqueued"] == 1
        finally:
            q.stop()

    def test_max_pending_drops_oldest(self):
        """队列满时丢最旧任务，保证内存有界。"""
        rec = Recorder()
        q = make_queue(lambda u, l: ("", MSG_ASR_NO_VOICE), rec, delays=(30.0,))
        q.start()
        try:
            for i in range(MAX_PENDING):
                assert q.enqueue("filehelper", i + 1, {}) is True
            assert q.enqueue("filehelper", MAX_PENDING + 1, {}) is True
            assert q.pending_count() == MAX_PENDING
            assert q.status()["dropped"] == 1
        finally:
            q.stop()


class TestServiceIntegration:
    def _svc(self, db: Database):
        from app.config import KEY_ANALYSIS_FILTER_SELF
        units: List[AnalysisUnit] = []
        agg = Aggregator(lambda u: units.append(u))
        svc = WeChatService(db, agg)
        svc._wxdb = object()      # 通过「微信库不可用」检查（不碰真实微信）
        # 语音链路测试的消息 sender_id=2（自己发送）；批次 5 需求 ⑤ 默认
        # 过滤自己消息，会拦截这些消息进不了语音转写分支——这里显式关闭。
        db.set_setting(KEY_ANALYSIS_FILTER_SELF, "0")
        return svc, units, agg

    def test_no_voice_failure_enqueues_and_records_local_id(self, monkeypatch):
        """取不到音频：日志带 voice_local_id，且任务进补转写队列。"""
        db = Database(":memory:")
        svc, units, agg = self._svc(db)
        monkeypatch.setattr(svc, "transcribe_voice",
                            lambda u, l: ("", MSG_ASR_NO_VOICE))
        svc._on_message(make_voice_msg(114))
        assert units == []                         # 尚未进入分析
        _total, rows = db.list_analysis_logs(page=1, page_size=10)
        assert rows[0]["status"] == "skipped"
        assert rows[0]["voice_local_id"] == 114
        assert "自动重试" in rows[0]["error"]
        assert svc._voice_retry.pending_count() == 1
        db.close()

    def test_non_retryable_failure_not_enqueued(self, monkeypatch):
        """开关关闭/模型未装这类失败重试无意义，不排队。"""
        db = Database(":memory:")
        svc, _units, agg = self._svc(db)
        monkeypatch.setattr(svc, "transcribe_voice",
                            lambda u, l: ("", MSG_ASR_DISABLED))
        svc._on_message(make_voice_msg(114))
        _total, rows = db.list_analysis_logs(page=1, page_size=10)
        assert rows[0]["status"] == "skipped"
        assert "自动重试" not in rows[0]["error"]
        assert svc._voice_retry.pending_count() == 0
        db.close()

    def test_retry_success_backfills_log_and_reanalyzes(self, monkeypatch):
        """补转写成功：原日志就地改为 ok + 文本，并重新进聚合窗口。"""
        db = Database(":memory:")
        svc, units, agg = self._svc(db)
        monkeypatch.setattr(svc, "transcribe_voice",
                            lambda u, l: ("", MSG_ASR_NO_VOICE))
        svc._on_message(make_voice_msg(114))
        log_id = db.list_analysis_logs(page=1, page_size=10)[1][0]["id"]
        # 模拟队列把任务取出来（快照与真实入队一致）
        svc._on_voice_retry_success(
            "filehelper", 114, "那家有机油滤芯",
            {"msg": {k: v for k, v in make_voice_msg(114).items()
                     if k != "content"},
             "kind": "friend", "sender_wxid": "", "sender_name": "我",
             "log_id": log_id}, 2)
        _total, rows = db.list_analysis_logs(page=1, page_size=10)
        assert rows[0]["status"] == "ok"
        assert rows[0]["input_text"] == "[语音] 那家有机油滤芯"
        assert "第 2 次重试" in rows[0]["error"]
        # 重新进合并窗口：显式 flush（测试不起调度线程）
        agg._flush_expired(force=True)
        assert len(units) == 1                     # 重新纳入分析
        assert "那家有机油滤芯" in units[0].merged_text()
        db.close()

    def test_retry_exhausted_marks_terminal_with_hint(self):
        """重试用尽：日志保留 skipped，文案指向「重新识别」。"""
        db = Database(":memory:")
        svc, _units, agg = self._svc(db)
        log_id = db.insert_analysis_log(
            session_username="filehelper", msg_count=1, input_text="[语音]",
            status="skipped", error="语音转写失败：音频未落库",
            voice_local_id=114)
        svc._on_voice_retry_failure(
            "filehelper", 114, MSG_ASR_NO_VOICE, {"log_id": log_id}, 5)
        _total, rows = db.list_analysis_logs(page=1, page_size=10)
        assert rows[0]["status"] == "skipped"
        assert "已自动重试 5 次" in rows[0]["error"]
        assert "重新识别" in rows[0]["error"]
        db.close()

    def test_manual_retry_success(self, monkeypatch):
        """手动重试：返回文本、回填日志、重新纳入分析。"""
        db = Database(":memory:")
        svc, units, agg = self._svc(db)
        log_id = db.insert_analysis_log(
            session_username="filehelper", msg_count=1, input_text="[语音]",
            status="skipped", error="语音转写失败：音频未落库",
            voice_local_id=114)
        monkeypatch.setattr(svc, "transcribe_voice",
                            lambda u, l: ("那家有机油滤芯", ""))
        monkeypatch.setattr(svc, "_rebuild_voice_msg",
                            lambda u, l, shown: {
                                "username": u, "type": "语音",
                                "content": shown, "local_id": l,
                                "create_time": time.time(), "sender_id": 2,
                                "sender_username": ""})
        text, err = svc.retry_voice("filehelper", 114, log_id)
        assert err == ""
        assert text == "那家有机油滤芯"
        _total, rows = db.list_analysis_logs(page=1, page_size=10)
        assert rows[0]["status"] == "ok"
        assert "手动重新识别" in rows[0]["error"]
        agg._flush_expired(force=True)
        assert len(units) == 1
        db.close()

    def test_manual_retry_failure_returns_reason(self, monkeypatch):
        db = Database(":memory:")
        svc, _units, agg = self._svc(db)
        monkeypatch.setattr(svc, "transcribe_voice",
                            lambda u, l: ("", MSG_ASR_NO_VOICE))
        text, err = svc.retry_voice("filehelper", 114)
        assert text == ""
        assert err == MSG_ASR_NO_VOICE
        db.close()

    def test_manual_retry_invalid_local_id(self):
        db = Database(":memory:")
        svc, _units, agg = self._svc(db)
        text, err = svc.retry_voice("filehelper", 0)
        assert text == ""
        assert "标识非法" in err
        db.close()


class TestDatabase:
    def test_update_analysis_log_voice(self):
        db = Database(":memory:")
        log_id = db.insert_analysis_log(
            session_username="filehelper", msg_count=1, input_text="[语音]",
            status="skipped", error="语音转写失败", voice_local_id=114)
        assert db.update_analysis_log_voice(
            log_id, input_text="[语音] 我要一台丰田发动机", status="ok",
            error="补转写成功") is True
        row = db.list_analysis_logs(page=1, page_size=10)[1][0]
        assert row["status"] == "ok"
        assert row["input_text"] == "[语音] 我要一台丰田发动机"
        assert row["error"] == "补转写成功"
        assert row["voice_local_id"] == 114
        db.close()

    def test_update_missing_log_returns_false(self):
        db = Database(":memory:")
        assert db.update_analysis_log_voice(
            999, input_text="x", status="ok", error="") is False
        assert db.update_analysis_log_voice(
            0, input_text="x", status="ok", error="") is False
        db.close()

    def test_voice_local_id_is_null_for_text_logs(self):
        db = Database(":memory:")
        db.insert_analysis_log(session_username="a", msg_count=1,
                               input_text="我要轮胎", status="ok")
        row = db.list_analysis_logs(page=1, page_size=10)[1][0]
        assert row["voice_local_id"] is None
        db.close()


def make_client(tmp_path, wechat) -> TestClient:
    db = Database(str(tmp_path / "voice.sqlite3"))
    deps.configure(db=db, wechat=wechat, pipeline=None, sender=None,
                   notifier=Notifier())
    app = FastAPI()
    app.include_router(system_router)
    return TestClient(app)


@pytest.fixture()
def cleanup_deps():
    yield
    deps.configure(db=None, wechat=None, pipeline=None, sender=None,
                   notifier=None)


class TestRetryApi:
    def test_ok_contract(self, tmp_path, cleanup_deps):
        db = Database(":memory:")
        units: List[AnalysisUnit] = []
        svc = WeChatService(db, Aggregator(lambda u: units.append(u)))
        svc.transcribe_voice = lambda u, l: ("那家有机油滤芯", "")
        client = make_client(tmp_path, svc)
        resp = client.post("/api/voice/retry",
                           json={"username": "filehelper", "local_id": 114})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "text": "那家有机油滤芯",
                               "error": ""}
        db.close()

    def test_failure_contract(self, tmp_path, cleanup_deps):
        db = Database(":memory:")
        units: List[AnalysisUnit] = []
        svc = WeChatService(db, Aggregator(lambda u: units.append(u)))
        svc.transcribe_voice = lambda u, l: ("", MSG_ASR_NO_VOICE)
        client = make_client(tmp_path, svc)
        resp = client.post("/api/voice/retry",
                           json={"username": "filehelper", "local_id": 114})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == MSG_ASR_NO_VOICE
        db.close()

    def test_internal_error_never_500(self, tmp_path, cleanup_deps):
        class Boom:
            def retry_voice(self, *args, **kwargs):
                raise RuntimeError("炸了")

        client = make_client(tmp_path, Boom())
        resp = client.post("/api/voice/retry",
                           json={"username": "filehelper", "local_id": 114})
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
        assert "稍后再试" in resp.json()["error"]
