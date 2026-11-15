# -*- coding: utf-8 -*-
"""「202 受理提示必须先于 WS 发送结果」的时序回归。

2026-11-15 18:26:26 实测事故：微信关到托盘后发送在 0.0 秒内 fail-closed，
后端日志里明明广播了「微信已关闭到系统托盘…」，前端却只停在「已加入发送
队列」。原因是 notifier.broadcast() 异步投递到主事件循环，而回复接口跑在
threadpool 里，结果比 202 响应更早到浏览器；随后 onSuccess 用受理文案把
失败提示覆盖掉了。

后端侧用 ACK_LEAD_SEC 给受理响应一点领先时间，前端侧用 settled 标记兜底。
本文件锁死后端这一半，避免以后有人「优化掉这点等待」把事故带回来。
"""
from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace

from app.sender import ACK_LEAD_SEC, SendTask, Sender, _Stages


def _task() -> SendTask:
    return SendTask(
        opportunity_id=170,
        session_username="12345@chatroom",
        session_name="家人群",
        text="测试消息",
    )


def _bare_sender() -> Sender:
    """不碰数据库/微信的 Sender 替身。"""
    return Sender(database=SimpleNamespace(),
                  notifier=SimpleNamespace(broadcast=lambda *a, **k: None))


def test_submit_stamps_enqueued_mono():
    """submit() 必须写入队时刻，否则 _run 的时序保护会被跳过。"""
    s = _bare_sender()
    s._queue = queue.Queue()
    task = _task()
    assert task.enqueued_mono == 0.0
    assert s.submit(task) is True
    assert task.enqueued_mono > 0.0
    assert s._queue.get_nowait() is task


def test_wait_ack_lead_waits_for_fresh_task(monkeypatch):
    """刚入队的任务：等待剩余的 ACK_LEAD_SEC，让 HTTP 202 先落地。"""
    s = _bare_sender()
    waits: list[float] = []
    monkeypatch.setattr(s._stop, "wait", lambda t: waits.append(t) or False)
    task = _task()
    task.enqueued_mono = time.monotonic()
    assert s._wait_ack_lead(task) is False
    assert len(waits) == 1
    assert 0 < waits[0] <= ACK_LEAD_SEC


def test_wait_ack_lead_skips_manually_built_task(monkeypatch):
    """测试/内部直接构造的任务不引入任何等待（老测试不受影响）。"""
    s = _bare_sender()
    waits: list[float] = []
    monkeypatch.setattr(s._stop, "wait", lambda t: waits.append(t) or False)
    assert s._wait_ack_lead(_task()) is False
    assert waits == []


def test_wait_ack_lead_skips_task_queued_long_ago(monkeypatch):
    """排队已超过领先窗口的任务（前面有慢任务）不再额外等待。"""
    s = _bare_sender()
    waits: list[float] = []
    monkeypatch.setattr(s._stop, "wait", lambda t: waits.append(t) or False)
    task = _task()
    task.enqueued_mono = time.monotonic() - ACK_LEAD_SEC - 1.0
    assert s._wait_ack_lead(task) is False
    assert waits == []


def test_wait_ack_lead_returns_true_when_stopping(monkeypatch):
    """停机信号要能立刻打断等待，且 _run 会据此放弃该任务。"""
    s = _bare_sender()
    monkeypatch.setattr(s._stop, "wait", lambda t: True)
    task = _task()
    task.enqueued_mono = time.monotonic()
    assert s._wait_ack_lead(task) is True


def test_zero_second_failure_is_broadcast_after_ack_window(monkeypatch):
    """端到端：0 秒失败的 send_result 不得早于受理响应窗口。"""
    sent: list[tuple[float, str, dict]] = []

    def broadcast(event, data):
        sent.append((time.monotonic(), event, data))

    s = Sender(database=SimpleNamespace(),
               notifier=SimpleNamespace(broadcast=broadcast))
    s._stop = threading.Event()
    done = threading.Event()

    def immediate_fail(task):
        # 模拟托盘态 ensure_visible() 立即返回 False 的 fail-closed 路径
        s._fail(task, "微信已关闭到系统托盘：请点击任务栏右下角的微信图标打开主窗口")
        done.set()

    monkeypatch.setattr(s, "_process", immediate_fail)
    task = _task()
    t0 = time.monotonic()
    s.submit(task)
    th = threading.Thread(target=s._run, daemon=True)
    th.start()
    assert done.wait(timeout=3), "发送线程未处理任务"
    s._stop.set()
    th.join(timeout=3)

    assert len(sent) == 1
    at, event, payload = sent[0]
    assert event == "send_result"
    assert payload["ok"] is False
    assert "托盘" in payload["message"]
    # 真实等待：结果必须落在受理窗口之后，不能再抢在 202 前面
    assert at - t0 >= ACK_LEAD_SEC * 0.8, (at - t0, ACK_LEAD_SEC)


def test_missing_gui_still_broadcasts_send_result(monkeypatch):
    """微信没启动/组件不可用时也必须结算任务，不能让前端一直「发送中」。

    ``_ensure_gui()`` 返回 None 只广播了全局 ``status_change``，旧代码在
    ``_process_locked`` 里直接 return，前端永远等不到 ``send_result``。
    """
    sent: list[tuple[str, dict]] = []

    def broadcast(event, data):
        sent.append((event, data))

    s = Sender(database=SimpleNamespace(),
               notifier=SimpleNamespace(broadcast=broadcast))
    monkeypatch.setattr(s, "_ensure_gui", lambda quiet=False: None)
    s._process_locked(_task(), _Stages())
    assert len(sent) == 1
    event, payload = sent[0]
    assert event == "send_result"
    assert payload["ok"] is False
    assert payload["opportunity_id"] == 170
    assert "微信" in payload["message"]
