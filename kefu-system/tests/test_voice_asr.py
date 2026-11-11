# -*- coding: utf-8 -*-
"""语音消息离线转写（app/voice_asr.py + 链路接入）单元测试。

测试策略（重要）：
- **绝不真实加载 228MB 模型**：`VoiceTranscriber` 的识别器一律用替身，
  解码/识别路径全部 patch；
- 不碰真实业务库与真实微信库（``:memory:`` / tmp_path）；
- 覆盖：依赖缺失降级、开关关闭、转写成功入聚合窗口、缓存命中不重复识别、
  批量缓存查询、不可清理约束、API 契约与下载幂等。

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_voice_asr.py -q
"""
from __future__ import annotations

import threading
import time
from typing import List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import content_format, voice_asr
from app.aggregator import Aggregator, AnalysisUnit
from app.api import deps
from app.api.router_system import router as system_router
from app.db import Database
from app.notifier import Notifier
from app.pipeline import Pipeline
from app.wechat_service import MSG_ASR_DISABLED, WeChatService


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def make_voice_msg(local_id: int = 10, username: str = "12345@chatroom",
                   content: str = "<msg><voicemsg length=\"3000\"/></msg>") -> dict:
    """构造一条语音消息回调 dict（微信 4.x type=语音 / local_type=34）。"""
    return {
        "local_id": local_id,
        "type": "语音",
        "sender_id": 3,
        "sender_username": "wxid_zhang",
        "create_time": time.time(),
        "content": content,
        "sort_seq": local_id,
        "username": username,
    }


def make_transcriber(tmp_path, *, model: bool = True, tokens: bool = True,
                     decoder: bool = True) -> voice_asr.VoiceTranscriber:
    """构造转写器，模型/解码器文件按开关创建空文件（不加载模型）。"""
    model_path = tmp_path / voice_asr.MODEL_FILE_NAME
    tokens_path = tmp_path / voice_asr.TOKENS_FILE_NAME
    decoder_path = tmp_path / "silk_v3_decoder.exe"
    if model:
        model_path.write_bytes(b"fake-onnx")
    if tokens:
        tokens_path.write_text("a 1\n", encoding="utf-8")
    if decoder:
        decoder_path.write_bytes(b"MZ")
    return voice_asr.VoiceTranscriber(
        model_path=model_path, tokens_path=tokens_path,
        decoder_path=decoder_path, max_seconds_provider=lambda: 180)


class FakeRecognizer:
    """识别器替身：create_stream/decode_stream 走通，返回固定文本。"""

    def __init__(self, text: str = "明天带病历过来") -> None:
        self.text = text
        self.decode_calls = 0

    def create_stream(self):
        return FakeStream()

    def decode_stream(self, stream) -> None:  # noqa: D102 - 替身
        self.decode_calls += 1
        stream.result.text = self.text


class FakeStream:
    """识别流替身（与 sherpa-onnx 2.0.1 一致：只有 accept_waveform）。"""

    def __init__(self) -> None:
        self.result = type("R", (), {"text": ""})()
        self.accepted: tuple = (0, [])

    def accept_waveform(self, sample_rate: int, samples) -> None:
        self.accepted = (sample_rate, samples)


@pytest.fixture(autouse=True)
def _reset_globals():
    """每个用例前后复位进程级单例与下载状态（避免用例互相污染）。"""
    voice_asr.reset_transcriber()
    voice_asr.reset_download_state()
    yield
    voice_asr.reset_transcriber()
    voice_asr.reset_download_state()


# ---------------------------------------------------------------------------
# 依赖检测：sherpa 不可导入 / 模型缺失 / 解码器缺失
# ---------------------------------------------------------------------------
class TestAvailability:
    def test_sherpa_not_importable(self, tmp_path, monkeypatch):
        transcriber = make_transcriber(tmp_path)
        monkeypatch.setattr(voice_asr, "sherpa_ready",
                            lambda: (False, "未安装语音识别库 sherpa-onnx（boom）"))
        ok, reason = transcriber.available()
        assert ok is False
        assert "sherpa-onnx" in reason
        # transcribe 不抛异常，直接返回中文原因
        text, err = transcriber.transcribe(str(tmp_path / "x.silk"))
        assert text == ""
        assert "sherpa-onnx" in err

    def test_model_missing(self, tmp_path):
        transcriber = make_transcriber(tmp_path, model=False)
        ok, reason = transcriber.available()
        assert ok is False
        assert "模型未安装" in reason
        text, err = transcriber.transcribe(str(tmp_path / "x.silk"))
        assert (text, err)[0] == ""
        assert "模型未安装" in err

    def test_decoder_missing(self, tmp_path):
        transcriber = make_transcriber(tmp_path, decoder=False)
        ok, reason = transcriber.available()
        assert ok is False
        assert "silk_v3_decoder.exe" in reason
        text, err = transcriber.transcribe(str(tmp_path / "x.silk"))
        assert text == ""
        assert "解码器" in err

    def test_all_ready_reports_available(self, tmp_path):
        transcriber = make_transcriber(tmp_path)
        assert transcriber.available() == (True, "")

    def test_missing_silk_file_is_reported(self, tmp_path):
        transcriber = make_transcriber(tmp_path)
        text, err = transcriber.transcribe(str(tmp_path / "nope.silk"))
        assert text == ""
        assert "语音文件不存在" in err


# ---------------------------------------------------------------------------
# 转写主流程（解码/识别全替身，不加载模型）
# ---------------------------------------------------------------------------
class TestTranscribe:
    def test_success_returns_text(self, tmp_path, monkeypatch):
        transcriber = make_transcriber(tmp_path)
        recognizer = FakeRecognizer("明天带病历过来")
        silk = tmp_path / "a.silk"
        silk.write_bytes(b"SILK")
        # 3 秒 PCM（16000 * 2 byte * 3）
        pcm = b"\x00\x01" * (16000 * 3)
        monkeypatch.setattr(transcriber, "_decode_silk", lambda p: pcm)
        monkeypatch.setattr(transcriber, "_get_recognizer", lambda: recognizer)
        text, err = transcriber.transcribe(str(silk))
        assert err == ""
        assert text == "明天带病历过来"
        assert recognizer.decode_calls == 1

    def test_too_long_voice_is_rejected(self, tmp_path, monkeypatch):
        transcriber = make_transcriber(tmp_path)
        silk = tmp_path / "long.silk"
        silk.write_bytes(b"SILK")
        # 200 秒 > 上限 180 秒
        pcm = b"\x00\x01" * (16000 * 200)
        monkeypatch.setattr(transcriber, "_decode_silk", lambda p: pcm)
        text, err = transcriber.transcribe(str(silk))
        assert text == ""
        assert "语音过长" in err

    def test_max_seconds_provider_failure_falls_back(self, tmp_path):
        transcriber = make_transcriber(tmp_path)
        transcriber.set_max_seconds_provider(lambda: "不是数字")
        assert transcriber.max_seconds() == voice_asr.MAX_SECONDS_DEFAULT

    def test_decode_failure_is_chinese_reason(self, tmp_path, monkeypatch):
        transcriber = make_transcriber(tmp_path)
        silk = tmp_path / "bad.silk"
        silk.write_bytes(b"SILK")

        def boom(_path):
            raise voice_asr._AsrError("语音解码失败（返回码 1）")

        monkeypatch.setattr(transcriber, "_decode_silk", boom)
        text, err = transcriber.transcribe(str(silk))
        assert text == ""
        assert "语音解码" in err

    def test_unexpected_exception_never_propagates(self, tmp_path, monkeypatch):
        transcriber = make_transcriber(tmp_path)
        silk = tmp_path / "bad.silk"
        silk.write_bytes(b"SILK")

        def boom(_path):
            raise RuntimeError("意外崩了")

        monkeypatch.setattr(transcriber, "_decode_silk", boom)
        text, err = transcriber.transcribe(str(silk))
        assert text == ""
        assert "语音解码失败" in err

    def test_empty_recognition_is_failure(self, tmp_path, monkeypatch):
        transcriber = make_transcriber(tmp_path)
        silk = tmp_path / "a.silk"
        silk.write_bytes(b"SILK")
        pcm = b"\x00\x01" * (16000 * 2)
        monkeypatch.setattr(transcriber, "_decode_silk", lambda p: pcm)
        monkeypatch.setattr(transcriber, "_get_recognizer",
                            lambda: FakeRecognizer(""))
        text, err = transcriber.transcribe(str(silk))
        assert text == ""
        assert "未识别到语音内容" in err


# ---------------------------------------------------------------------------
# db：voice_texts 缓存 DAO
# ---------------------------------------------------------------------------
class TestVoiceTextDao:
    def test_save_and_get_single(self):
        db = Database(":memory:")
        assert db.get_voice_text("g@chatroom", 1) is None
        db.save_voice_text("g@chatroom", 1, text="你好", status="ok",
                           duration_ms=520)
        row = db.get_voice_text("g@chatroom", 1)
        assert row["text"] == "你好"
        assert row["status"] == "ok"
        assert row["duration_ms"] == 520
        assert row["created_at"]
        # 覆盖写（主键 username+local_id）
        db.save_voice_text("g@chatroom", 1, text="改过了", status="ok")
        assert db.get_voice_text("g@chatroom", 1)["text"] == "改过了"
        assert db.get_voice_text("other@chatroom", 1) is None
        db.close()

    def test_batch_query(self):
        db = Database(":memory:")
        for local_id in (1, 2, 3):
            db.save_voice_text("g@chatroom", local_id, text=f"文本{local_id}",
                               status="ok")
        result = db.get_voice_texts("g@chatroom", [1, 3, 99])
        assert set(result) == {1, 3}
        assert result[1]["text"] == "文本1"
        assert result[3]["text"] == "文本3"
        assert db.get_voice_texts("g@chatroom", []) == {}
        assert db.get_voice_texts("h@chatroom", [1]) == {}
        db.close()

    def test_voice_texts_not_purgable(self):
        """voice_texts 不在清理白名单：delete_older_than 必须拒绝。"""
        db = Database(":memory:")
        db.save_voice_text("g@chatroom", 1, text="你好", status="ok")
        with pytest.raises(ValueError):
            db.delete_older_than("voice_texts", 1)
        with pytest.raises(ValueError):
            db.count_older_than("voice_texts", 1)
        # 拒绝清理后数据仍在
        assert db.get_voice_text("g@chatroom", 1)["text"] == "你好"
        # 原有白名单不受影响
        assert db.delete_older_than("analysis_logs", 3650) >= 0
        db.close()


# ---------------------------------------------------------------------------
# wechat_service：开关 / 转写 / 缓存
# ---------------------------------------------------------------------------
class TestVoiceService:
    def _svc(self, db: Database):
        """构造 WeChatService（聚合窗口不 flush，由用例显式触发）。"""
        units: List[AnalysisUnit] = []
        agg = Aggregator(lambda u: units.append(u))
        svc = WeChatService(db, agg)
        svc._wxdb = object()      # 让「微信库不可用」检查通过（不触碰真实微信）
        return svc, units, agg

    def test_switch_off_keeps_skipped(self):
        db = Database(":memory:")
        db.set_setting("asr.enabled", "0")
        svc, units, _agg = self._svc(db)
        assert svc.transcribe_voice("12345@chatroom", 10) == (
            "", MSG_ASR_DISABLED)
        svc._on_message(make_voice_msg(10))
        assert units == []        # 不进聚合窗口
        total, rows = db.list_analysis_logs(page=1, page_size=10)
        assert total == 1
        assert rows[0]["status"] == "skipped"
        assert "语音" in rows[0]["error"]
        db.close()

    def test_unavailable_model_keeps_skipped(self, tmp_path, monkeypatch):
        db = Database(":memory:")
        svc, units, _agg = self._svc(db)
        stub = make_transcriber(tmp_path, model=False)
        monkeypatch.setattr(voice_asr, "get_transcriber", lambda: stub)
        text, err = svc.transcribe_voice("12345@chatroom", 10)
        assert text == ""
        assert "模型未安装" in err
        svc._on_message(make_voice_msg(10))
        assert units == []
        total, rows = db.list_analysis_logs(page=1, page_size=10)
        assert total == 1 and rows[0]["status"] == "skipped"
        assert "语音转写失败" in rows[0]["error"]
        # 失败不写缓存（下次可重试）
        assert db.get_voice_text("12345@chatroom", 10) is None
        db.close()

    def test_success_enters_aggregator(self, tmp_path, monkeypatch):
        db = Database(":memory:")
        svc, units, agg = self._svc(db)
        stub = make_transcriber(tmp_path)
        monkeypatch.setattr(voice_asr, "get_transcriber", lambda: stub)
        monkeypatch.setattr(stub, "transcribe", lambda p: ("明天带病历过来", ""))
        silk = tmp_path / "10.silk"
        silk.write_bytes(b"SILK")
        monkeypatch.setattr(svc, "_download_voice_silk", lambda u, i: str(silk))
        svc._on_message(make_voice_msg(10))
        agg._flush_expired(force=True)
        assert len(units) == 1
        _hm, content = units[0].lines[0]
        assert content == "[语音] 明天带病历过来"
        assert units[0].local_ids == [10]
        # 缓存已落库
        row = db.get_voice_text("12345@chatroom", 10)
        assert row and row["text"] == "明天带病历过来" and row["status"] == "ok"
        db.close()

    def test_cache_hit_avoids_second_recognition(self, tmp_path, monkeypatch):
        db = Database(":memory:")
        svc, _units, _agg = self._svc(db)
        stub = make_transcriber(tmp_path)
        calls = {"transcribe": 0, "download": 0}

        def fake_transcribe(_path):
            calls["transcribe"] += 1
            return "缓存的文本", ""

        monkeypatch.setattr(voice_asr, "get_transcriber", lambda: stub)
        monkeypatch.setattr(stub, "transcribe", fake_transcribe)
        monkeypatch.setattr(svc, "_download_voice_silk",
                            lambda u, i: calls.__setitem__("download",
                                                           calls["download"] + 1)
                            or str(tmp_path / "x.silk"))
        first = svc.transcribe_voice("12345@chatroom", 10)
        second = svc.transcribe_voice("12345@chatroom", 10)
        assert first == ("缓存的文本", "")
        assert second == ("缓存的文本", "")
        assert calls == {"transcribe": 1, "download": 1}
        db.close()

    def test_duration_ms_excludes_model_load(self, tmp_path, monkeypatch):
        """duration_ms 只算解码+识别，首次模型加载时间不计入（避免统计虚高）。"""
        db = Database(":memory:")
        svc, _units, _agg = self._svc(db)
        stub = make_transcriber(tmp_path)

        def slow_load():
            time.sleep(0.3)          # 模拟首次加载模型（真实 2.5~11s）
            return FakeRecognizer("好了")

        monkeypatch.setattr(voice_asr, "get_transcriber", lambda: stub)
        monkeypatch.setattr(stub, "_decode_silk",
                            lambda p: b"\x00\x01" * 16000)
        monkeypatch.setattr(stub, "_get_recognizer", slow_load)
        silk = tmp_path / "10.silk"
        silk.write_bytes(b"SILK")
        monkeypatch.setattr(svc, "_download_voice_silk", lambda u, i: str(silk))
        text, err = svc.transcribe_voice("12345@chatroom", 10)
        assert (text, err) == ("好了", "")
        timing = stub.last_timing()
        assert timing["model_load_ms"] >= 250   # 加载时间被单独记录
        row = db.get_voice_text("12345@chatroom", 10)
        assert row["duration_ms"] < 250         # 但没写进 duration_ms
        db.close()

    def test_fetch_messages_fills_cached_voice_text(self):
        """消息列表只读缓存（绝不在请求线程里识别）。"""
        db = Database(":memory:")
        svc, _units, _agg = self._svc(db)
        from tests.test_core import FakeWxDb  # 复用既有替身（含 local_id=1）
        svc._wxdb = FakeWxDb()
        svc._wxdb.msgs = [{"local_id": 1, "type": "语音",
                           "sender_username": "wxid_a",
                           "create_time": 1700000000,
                           "content": "<msg><voicemsg/></msg>",
                           "sort_seq": 10}]
        db.save_voice_text("12345@chatroom", 1, text="已经转好了", status="ok")
        msgs, err = svc.get_messages_with_error("12345@chatroom", limit=10)
        assert err == ""
        assert msgs[0]["content"] == "[语音] 已经转好了"
        # 未命中缓存时保持占位
        db2 = Database(":memory:")
        svc2, _u2, _a2 = self._svc(db2)
        svc2._wxdb = svc._wxdb
        msgs2, _err2 = svc2.get_messages_with_error("12345@chatroom", limit=10)
        assert msgs2[0]["content"] == "[语音]"
        db.close()
        db2.close()


# ---------------------------------------------------------------------------
# pipeline：分析时补转写（最多 3 条，失败不影响主流程）
# ---------------------------------------------------------------------------
class TestPipelineTopup:
    def _unit(self, lines, local_ids=None) -> AnalysisUnit:
        return AnalysisUnit(
            session_username="12345@chatroom", session_name="家长群",
            session_kind="group", sender_wxid="wxid_zhang", sender_name="张三",
            window_start="2026-11-02T14:02:00", window_end="2026-11-02T14:05:00",
            merged_count=len(lines), lines=list(lines),
            local_ids=list(local_ids or []))

    def test_topup_replaces_placeholder(self):
        db = Database(":memory:")
        pipeline = Pipeline(db, Notifier())
        calls: List[tuple] = []

        def lookup(username, local_id):
            calls.append((username, local_id))
            return f"语音内容{local_id}", ""

        pipeline.set_voice_lookup(lookup)
        unit = self._unit([("14:02", "[语音]"), ("14:03", "有谁能做CNC")], [1, 2])
        pipeline._topup_voice_lines(unit)
        assert unit.lines == [("14:02", "[语音] 语音内容1"),
                              ("14:03", "有谁能做CNC")]
        assert calls == [("12345@chatroom", 1)]
        assert "语音内容1" in unit.merged_text()
        db.close()

    def test_topup_capped_at_three(self):
        db = Database(":memory:")
        pipeline = Pipeline(db, Notifier())
        calls: List[int] = []

        def lookup(_username, local_id):
            calls.append(local_id)
            return f"t{local_id}", ""

        pipeline.set_voice_lookup(lookup)
        lines = [("14:0%d" % i, "[语音]") for i in range(6)]
        unit = self._unit(lines, list(range(1, 7)))
        pipeline._topup_voice_lines(unit)
        assert len(calls) == 3                 # 单次最多补 3 条
        assert unit.lines[0][1] == "[语音] t1"
        assert unit.lines[3][1] == "[语音]"    # 第 4 条保持占位
        db.close()

    def test_topup_counts_failed_attempts_too(self):
        """10 条语音全部转写失败：lookup 也只应被调用 3 次（尝试计入预算）。

        若只对成功扣减，模型未装/开关关闭时一次 10 条语音会连做 10 次
        尝试（每次最坏 1.4s），把分析线程拖住十几秒。
        """
        db = Database(":memory:")
        pipeline = Pipeline(db, Notifier())
        calls: List[int] = []

        def lookup(_username, local_id):
            calls.append(local_id)
            return "", "语音识别模型未安装"

        pipeline.set_voice_lookup(lookup)
        lines = [("14:%02d" % i, "[语音]") for i in range(10)]
        unit = self._unit(lines, list(range(1, 11)))
        pipeline._topup_voice_lines(unit)
        assert len(calls) == 3                 # 尝试也计入预算
        assert len(unit.lines) == 10           # 行数不变
        assert [text for _hm, text in unit.lines] == ["[语音]"] * 10
        db.close()

    def test_topup_failure_keeps_analysis_alive(self):
        db = Database(":memory:")
        pipeline = Pipeline(db, Notifier())

        def lookup(_username, _local_id):
            raise RuntimeError("转写炸了")

        pipeline.set_voice_lookup(lookup)
        unit = self._unit([("14:02", "[语音]")], [1])
        pipeline._topup_voice_lines(unit)       # 不抛异常
        assert unit.lines == [("14:02", "[语音]")]
        db.close()

    def test_no_lookup_is_noop(self):
        db = Database(":memory:")
        pipeline = Pipeline(db, Notifier())
        unit = self._unit([("14:02", "[语音]")], [1])
        pipeline._topup_voice_lines(unit)
        assert unit.lines == [("14:02", "[语音]")]
        db.close()


# ---------------------------------------------------------------------------
# 下载：幂等 + 状态机（网络请求全部替身）
# ---------------------------------------------------------------------------
class TestDownload:
    def test_start_is_idempotent_while_downloading(self, tmp_path, monkeypatch):
        gate = threading.Event()
        counter = {"ensured": 0, "threads": 0}

        def slow_ensure(progress_cb=None):
            counter["ensured"] += 1
            counter["threads"] += 1
            gate.wait(5)
            return True, ""

        monkeypatch.setattr(voice_asr, "ensure_model", slow_ensure)
        monkeypatch.setattr(voice_asr, "download_decoder",
                            lambda progress_cb=None: (True, ""))
        monkeypatch.setattr(voice_asr, "model_ready", lambda: False)
        monkeypatch.setattr(voice_asr, "model_size_bytes", lambda: 123)

        first = voice_asr.start_model_download()
        second = voice_asr.start_model_download()
        assert first["status"] == voice_asr.STATUS_DOWNLOADING
        assert second["status"] == voice_asr.STATUS_DOWNLOADING
        assert counter["ensured"] == 1          # 只起了一个下载任务
        gate.set()
        deadline = time.time() + 5
        while voice_asr.download_state()["status"] == \
                voice_asr.STATUS_DOWNLOADING and time.time() < deadline:
            time.sleep(0.02)
        assert voice_asr.download_state()["status"] == voice_asr.STATUS_READY

    def test_download_failure_reports_error(self, monkeypatch):
        monkeypatch.setattr(voice_asr, "ensure_model",
                            lambda progress_cb=None: (False, "网络不可用"))
        monkeypatch.setattr(voice_asr, "download_decoder",
                            lambda progress_cb=None: (True, ""))
        monkeypatch.setattr(voice_asr, "model_ready", lambda: False)
        voice_asr.start_model_download()
        deadline = time.time() + 5
        while voice_asr.download_state()["status"] == \
                voice_asr.STATUS_DOWNLOADING and time.time() < deadline:
            time.sleep(0.02)
        state = voice_asr.download_state()
        assert state["status"] == voice_asr.STATUS_FAILED
        assert "网络不可用" in state["error"]

    def test_value_error_in_download_becomes_error_state(self, monkeypatch):
        def boom(progress_cb=None):
            raise ValueError("磁盘满了")

        monkeypatch.setattr(voice_asr, "ensure_model", boom)
        monkeypatch.setattr(voice_asr, "download_decoder",
                            lambda progress_cb=None: (True, ""))
        monkeypatch.setattr(voice_asr, "model_ready", lambda: False)
        voice_asr.start_model_download()
        deadline = time.time() + 5
        while voice_asr.download_state()["status"] == \
                voice_asr.STATUS_DOWNLOADING and time.time() < deadline:
            time.sleep(0.02)
        state = voice_asr.download_state()
        assert state["status"] == voice_asr.STATUS_FAILED
        assert "磁盘满了" in state["error"]


# ---------------------------------------------------------------------------
# API 契约
# ---------------------------------------------------------------------------
def make_client(tmp_path) -> tuple[TestClient, Database]:
    db = Database(str(tmp_path / "asr.sqlite3"))
    deps.configure(db=db, wechat=None, pipeline=None, sender=None,
                   notifier=Notifier())
    app = FastAPI()
    app.include_router(system_router)
    return TestClient(app), db


ASR_FIELDS = {"enabled", "available", "reason", "engine", "model_dir",
              "model_ready", "model_size_bytes", "decoder_ready",
              "download_status", "downloaded_bytes", "total_bytes", "error"}


@pytest.fixture()
def cleanup_deps():
    yield
    deps.configure(db=None, wechat=None, pipeline=None, sender=None,
                   notifier=None)


class TestAsrApi:
    def test_get_contract(self, tmp_path, cleanup_deps):
        client, db = make_client(tmp_path)
        try:
            resp = client.get("/api/system/asr")
            assert resp.status_code == 200
            body = resp.json()
            assert set(body) == ASR_FIELDS
            assert body["engine"] == voice_asr.ENGINE_NAME
            assert body["enabled"] is True           # 默认开启
            assert isinstance(body["model_size_bytes"], int)
            assert body["download_status"] in ("idle", "downloading",
                                               "ready", "failed")
        finally:
            db.close()

    def test_put_toggles_setting(self, tmp_path, cleanup_deps):
        client, db = make_client(tmp_path)
        try:
            resp = client.put("/api/system/asr", json={"enabled": False})
            assert resp.status_code == 200
            assert resp.json()["enabled"] is False
            assert db.get_setting("asr.enabled") == "0"
            resp2 = client.put("/api/system/asr", json={"enabled": True})
            assert resp2.json()["enabled"] is True
            assert db.get_setting("asr.enabled") == "1"
        finally:
            db.close()

    def test_download_endpoint_idempotent(self, tmp_path, cleanup_deps,
                                          monkeypatch):
        """重复 POST 不会重复起下载任务（进度照原样返回）。"""
        gate = threading.Event()
        ensured = {"count": 0}

        def slow_ensure(progress_cb=None):
            ensured["count"] += 1
            gate.wait(5)
            return True, ""

        monkeypatch.setattr(voice_asr, "ensure_model", slow_ensure)
        monkeypatch.setattr(voice_asr, "download_decoder",
                            lambda progress_cb=None: (True, ""))
        monkeypatch.setattr(voice_asr, "model_ready", lambda: False)
        monkeypatch.setattr(voice_asr, "model_size_bytes", lambda: 0)
        client, db = make_client(tmp_path)
        try:
            first = client.post("/api/system/asr/download")
            second = client.post("/api/system/asr/download")
            assert first.status_code == 200 and second.status_code == 200
            assert first.json()["download_status"] == "downloading"
            assert second.json()["download_status"] == "downloading"
            assert ensured["count"] == 1        # 只起了一个下载任务
            gate.set()
            deadline = time.time() + 5
            while voice_asr.download_state()["status"] == \
                    voice_asr.STATUS_DOWNLOADING and time.time() < deadline:
                time.sleep(0.02)
            assert voice_asr.download_state()["status"] == \
                voice_asr.STATUS_READY
        finally:
            gate.set()
            db.close()

    def test_status_survives_probe_exception(self, tmp_path, cleanup_deps,
                                            monkeypatch):
        class Boom:
            def available(self):
                raise RuntimeError("探测炸了")

        monkeypatch.setattr(voice_asr, "get_transcriber", lambda: Boom())
        client, db = make_client(tmp_path)
        try:
            resp = client.get("/api/system/asr")
            assert resp.status_code == 200
            assert resp.json()["available"] is False
            assert resp.json()["engine"] == voice_asr.ENGINE_NAME
        finally:
            db.close()

    def test_download_exception_is_not_500(self, tmp_path, cleanup_deps,
                                           monkeypatch):
        def boom() -> dict:
            raise RuntimeError("线程创建失败")

        monkeypatch.setattr(voice_asr, "start_model_download", boom)
        client, db = make_client(tmp_path)
        try:
            resp = client.post("/api/system/asr/download")
            assert resp.status_code == 200
            assert set(resp.json()) == ASR_FIELDS
        finally:
            db.close()


# ---------------------------------------------------------------------------
# content_format 语音文本工具
# ---------------------------------------------------------------------------
class TestContentFormatVoice:
    def test_helpers(self):
        assert content_format.is_voice_type("语音") is True
        assert content_format.is_voice_type("voice") is True
        assert content_format.is_voice_type("文本") is False
        assert content_format.voice_needs_text("[语音]") is True
        assert content_format.voice_needs_text(" [语音] ") is True
        assert content_format.voice_needs_text("[语音] 你好") is False
        assert content_format.voice_text_content("你好") == "[语音] 你好"
        assert content_format.voice_text_content("") == "[语音]"
        # 原有非文本格式化口径不变
        assert content_format.format_content("语音", "<msg/>") == "[语音]"
