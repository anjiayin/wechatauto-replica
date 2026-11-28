# -*- coding: utf-8 -*-
"""解密缓存晋升前「主动释放本进程只读连接」的回归（2026-11-28 打包版 1.4.2 实测）。

现场症状
--------
运行日志反复出现::

    contact__contact.db 晋升失败原因: PermissionError: [WinError 5] 拒绝访问。:
      '...\\contact__contact.db.rebuild' -> '...\\contact__contact.db'（重试 60 次仍未释放）
    contact__contact.db 缓存重建完成但晋升失败（正式缓存被占用？），耗时 12.5s，下轮重试

根因（本文件第 1~3 组用例逐条锁死）
----------------------------------
1. SQLite 在 Windows 上打开库文件时不带 ``FILE_SHARE_DELETE``，只要本进程还有
   一个只读连接挂着，``os.replace`` 就必然 WinError 5 —— 实测一次连接即可复现；
2. 靠「等对方自己关」在轮询高峰期等不到空档：实测 contact 的 .db/-wal/-shm
   被同一进程连续握住 7s 以上，而窗口只有 60×0.2s；
3. 白烧的代价不止那 12s：stamp 不更新 → 每轮轮询再触发一次整库重建，
   把 I/O 抢得更凶。

修复：晋升先按原样被动等几次，等不到就主动制造空档（挡住新读连接 + 关闭已
登记的读连接）；仍失败则该分片退避，不再反复重建。被打断的查询按「重开
再查」处理，绝不误判成库损坏。

全部离线临时文件，不触碰真实微信库。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time

# 仓库根（wechatauto 包父级）——与其他 db 相关用例同一写法
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import pytest

from wechatauto import db as dbmod
from wechatauto.db import WeChatDB


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _make_db(path: str, tag: str) -> str:
    """建一个真 sqlite 库，返回路径（内容用 tag 区分，便于验证替换是否生效）。"""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (?)", (tag,))
    conn.commit()
    conn.close()
    return path


def _rows(path: str):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT x FROM t").fetchall()
    finally:
        conn.close()


@pytest.fixture
def cache_pair(tmp_path):
    """正式缓存 dst + 待晋升的 .rebuild 临时缓存 tmp。"""
    dst = str(tmp_path / "contact__contact.db")
    _make_db(dst, "OLD")
    tmp = dst + ".rebuild"
    _make_db(tmp, "NEW")
    return tmp, dst


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """重试间隔归零，并把被动阶段调短，测试不真等 0.2s×N。"""
    monkeypatch.setattr(dbmod, "PROMOTE_RETRY_INTERVAL_SEC", 0.0)
    yield
    # 名册/暂停集合是模块级的：先摘干净再断言，避免一处泄漏把后续用例全拖崩
    leaked = set(dbmod._PROMOTING_PATHS)
    dbmod._PROMOTING_PATHS.clear()
    dbmod._RO_LEASES.clear()
    assert not leaked, "晋升结束后仍留有暂停标记：%s" % leaked


def _fake_db(**attrs):
    """按 __new__ 构造 WeChatDB 替身（与仓库既有 cache 用例同一手法）。"""
    db = object.__new__(WeChatDB)
    db._rebuild_guard = threading.Lock()
    db._rebuilding = set()
    db._rebuild_locks = {}
    db._rebuild_done_at = {}
    for k, v in attrs.items():
        setattr(db, k, v)
    return db


# ---------------------------------------------------------------------------
# 1. 登记表：连接打开即登记，close 即摘除
# ---------------------------------------------------------------------------
class TestRoLeaseRegistry:
    def test_open_registers_and_close_unregisters(self, cache_pair):
        _, dst = cache_pair
        key = dbmod._lease_key(dst)
        conn = WeChatDB._connect_ro(dst)
        assert len(dbmod._RO_LEASES.get(key, [])) == 1
        conn.close()
        assert key not in dbmod._RO_LEASES

    def test_returned_conn_is_still_a_sqlite_connection(self, cache_pair):
        """换成子类不能破坏调用方的类型预期（row_factory 等仍生效）。"""
        _, dst = cache_pair
        conn = WeChatDB._connect_ro(dst)
        try:
            assert isinstance(conn, sqlite3.Connection)
            row = conn.execute("SELECT * FROM t").fetchone()
            assert row["x"] == "OLD"          # sqlite3.Row 语义保留
        finally:
            conn.close()

    def test_double_close_is_idempotent(self, cache_pair):
        _, dst = cache_pair
        conn = WeChatDB._connect_ro(dst)
        conn.close()
        conn.close()                          # 重复 close 不应二次摘册/抛错
        assert dbmod._lease_key(dst) not in dbmod._RO_LEASES

    def test_leaked_conns_are_capped_not_piled_up(self, monkeypatch, tmp_path):
        """sqlite3.Connection 不支持弱引用：忘 close 的连接靠上限回收，
        否则名册会把泄漏的连接一直留着，句柄永远不放。"""
        monkeypatch.setattr(dbmod, "MAX_RO_LEASES_PER_PATH", 2)
        dst = _make_db(str(tmp_path / "message_8.db"), "OLD")
        leaked = [WeChatDB._connect_ro(dst) for _ in range(4)]
        key = dbmod._lease_key(dst)
        assert len(dbmod._RO_LEASES.get(key, [])) == 2
        for c in leaked[:2]:                  # 最旧的两个已被回收
            with pytest.raises(sqlite3.ProgrammingError):
                c.execute("SELECT 1").fetchone()
        for c in leaked[2:]:
            c.close()


# ---------------------------------------------------------------------------
# 2. 真机语义：只读连接确实会挡住 os.replace，释放后立刻换得上
# ---------------------------------------------------------------------------
class TestPromoteReleasesWindowsHandles:
    @pytest.mark.skipif(os.name != "nt", reason="FILE_SHARE_DELETE 语义仅 Windows")
    def test_real_windows_replace_blocked_then_succeeds(self, cache_pair):
        """不释放就替换不了（复现日志里的 WinError 5），修复后自动换上新缓存。"""
        tmp, dst = cache_pair
        conn = WeChatDB._connect_ro(dst)
        with pytest.raises(PermissionError):
            os.replace(tmp, dst)              # 前提：一次只读连接即可锁死晋升
        assert WeChatDB._promote_cache(tmp, dst) is True
        assert _rows(dst) == [("NEW",)]       # 正式缓存真的被换掉了
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1").fetchone()   # 释放是故意的，代价见第 4 组

    def test_promote_closes_registered_reader(self, monkeypatch, cache_pair):
        """跨平台等价用例：占用语义为假（有登记连接就报 WinError 5），
        验证「主动释放 → 替换成功」这条链路本身。"""
        tmp, dst = cache_pair
        key = dbmod._lease_key(dst)
        monkeypatch.setattr(dbmod, "PROMOTE_PASSIVE_ATTEMPTS", 0)
        real_replace = os.replace

        def blocked_if_leased(src, target):
            if dbmod._RO_LEASES.get(key):
                raise PermissionError(5, "拒绝访问。")
            return real_replace(src, target)

        monkeypatch.setattr(os, "replace", blocked_if_leased)
        conn = WeChatDB._connect_ro(dst)      # 模拟轮询正在读这份缓存
        assert WeChatDB._promote_cache(tmp, dst) is True
        assert _rows(dst) == [("NEW",)]
        assert key not in dbmod._RO_LEASES
        assert conn not in dbmod._RO_LEASES.get(key, [])

    def test_passive_phase_does_not_disturb_readers(self, monkeypatch, cache_pair):
        """前几轮纯等待：绝不能提前关掉别人正在用的连接。"""
        tmp, dst = cache_pair
        key = dbmod._lease_key(dst)
        monkeypatch.setattr(dbmod, "PROMOTE_PASSIVE_ATTEMPTS", 3)
        monkeypatch.setattr(dbmod, "PROMOTE_ACTIVE_ATTEMPTS", 2)
        leased = []
        attempts = []

        def always_blocked(src, target):
            leased.append(len(dbmod._RO_LEASES.get(key, [])))
            attempts.append(src)
            raise PermissionError(5, "拒绝访问。")

        monkeypatch.setattr(os, "replace", always_blocked)
        conn = WeChatDB._connect_ro(dst)
        assert WeChatDB._promote_cache(tmp, dst) is False
        assert leased[:3] == [1, 1, 1], "被动阶段不该释放读连接"
        assert leased[3] == 0, "进入主动阶段后必须已释放"
        assert len(leased) == 5               # 3 被动 + 2 主动后收手，不再空转
        assert not os.path.exists(tmp), "失败必须清掉临时文件"
        assert os.path.exists(dst), "失败必须保留旧缓存"
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1").fetchone()


# ---------------------------------------------------------------------------
# 3. 晋升期间新读连接让路（有超时上限）
# ---------------------------------------------------------------------------
class TestPromotePause:
    def test_new_reader_waits_then_proceeds_after_resume(self, cache_pair):
        _, dst = cache_pair
        dbmod._promote_pause(dst)
        done = threading.Event()

        def reader():
            conn = WeChatDB._connect_ro(dst)
            conn.close()
            done.set()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        assert not done.wait(0.2), "晋升期间不该立刻放行"
        dbmod._promote_resume(dst)
        assert done.wait(1.0), "resume 后读连接应被唤醒"
        t.join(2.0)

    def test_reader_wait_is_bounded(self, monkeypatch, cache_pair):
        """卡住的晋升不能把轮询拖死：到点自己放行，本次晋升交给退避。"""
        _, dst = cache_pair
        monkeypatch.setattr(dbmod, "PROMOTE_READERS_WAIT_SEC", 0.15)
        dbmod._promote_pause(dst)
        started = time.monotonic()
        dbmod._wait_promote_slot(dst)
        assert time.monotonic() - started < 1.0
        dbmod._promote_resume(dst)


# ---------------------------------------------------------------------------
# 4. 跨线程释放：这是修复能不能生效的分水岭
# ---------------------------------------------------------------------------
class TestCrossThreadRelease:
    def test_release_closes_conn_owned_by_another_thread(self, cache_pair):
        """轮询线程持有的连接，必须由重建线程关得掉。

        默认 ``check_same_thread=True`` 时跨线程 close 直接抛 ProgrammingError，
        ``_release_ro_leases`` 一个都关不掉 —— 实测这种写法下最坏场景仍是
        0/8 成功、每次白烧 12.1s，等于没修。
        """
        _, dst = cache_pair
        box = {}
        ready = threading.Event()
        hold = threading.Event()

        def owner():
            box["conn"] = WeChatDB._connect_ro(dst)
            ready.set()
            hold.wait(5)                    # 线程存活期间一直挂着句柄

        t = threading.Thread(target=owner, daemon=True)
        t.start()
        assert ready.wait(2)
        try:
            assert dbmod._release_ro_leases(dst) == 1, "跨线程必须真的关掉"
            with pytest.raises(sqlite3.ProgrammingError):
                box["conn"].execute("SELECT 1").fetchone()
        finally:
            hold.set()
            t.join(3)

    def test_unclosable_lease_stays_registered(self, monkeypatch, cache_pair):
        """关不掉的连接留在名册里等下一轮：悄悄摘除等于把句柄变成无人看管。"""
        _, dst = cache_pair
        conn = WeChatDB._connect_ro(dst)
        key = dbmod._lease_key(dst)

        def refuse(self):
            raise sqlite3.OperationalError("unable to close due to unfinalized statements")

        monkeypatch.setattr(dbmod._RoCacheConn, "close", refuse, raising=False)
        assert dbmod._release_ro_leases(dst) == 0
        assert any(c is conn for c in dbmod._RO_LEASES.get(key, []))
        monkeypatch.undo()
        conn.close()


# ---------------------------------------------------------------------------
# 5. 被打断的查询按「重开再查」处理，不误判为库损坏
# ---------------------------------------------------------------------------
class TestInterruptedQueryRetry:
    def test_closed_by_promote_reopens_instead_of_invalidating(self):
        db = _fake_db(_last_found_rels=["contact/contact.db"])
        calls = {"open": 0, "invalidate": 0}

        class Boom:
            def __init__(self):
                self.n = 0

            def close(self):
                pass

        conn = Boom()

        def msg_conn(user):
            calls["open"] += 1
            return [(conn, "Msg_0")]

        def build(found):
            calls["open_check"] = calls["open"]
            if calls["open"] == 1:
                raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
            return ["ok"]

        db._msg_conn = msg_conn
        db._invalidate_single_cache = lambda rel: calls.__setitem__(
            "invalidate", calls["invalidate"] + 1)
        db._mark_bad_shard = lambda rel: None
        assert db._run_msg_query("111@chatroom", build) == ["ok"]
        assert calls["open"] == 2, "应重开连接重试一次"
        assert calls["invalidate"] == 0, "连接被释放不是缓存损坏，别失效重建"

    def test_still_raises_after_second_interruption(self):
        db = _fake_db()
        conn = type("C", (), {"close": lambda self: None})()
        db._msg_conn = lambda user: [(conn, "Msg_0")]
        db._invalidate_single_cache = lambda rel: None
        db._mark_bad_shard = lambda rel: None

        def build(found):
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")

        with pytest.raises(sqlite3.ProgrammingError):
            db._run_msg_query("111@chatroom", build)

    def test_malformed_path_untouched(self):
        """真正的库损坏仍走原逻辑：失效 + 标坏分片 + 重建。"""
        db = _fake_db(_last_found_rels=["message/message_8.db"])
        conn = type("C", (), {"close": lambda self: None})()
        invalid = []
        db._msg_conn = lambda user: [(conn, "Msg_0")]
        db._invalidate_single_cache = invalid.append
        db._mark_bad_shard = lambda rel: None

        def build(found):
            if not invalid:
                raise sqlite3.DatabaseError(
                    "database disk image is malformed")
            return ["ok"]

        assert db._run_msg_query("111@chatroom", build) == ["ok"]
        assert invalid == ["message/message_8.db"]


# ---------------------------------------------------------------------------
# 6. 退避：晋升失败后不再每轮白烧整库重建
# ---------------------------------------------------------------------------
class TestPromoteFailBackoff:
    def test_kick_rebuild_skipped_during_backoff(self, monkeypatch):
        db = _fake_db()
        starts = []
        monkeypatch.setattr(dbmod.threading, "Thread",
                            lambda **kw: starts.append(kw) or _NoopThread())
        args = ("contact/contact.db", "src", "dst", b"k", 1.0, 2, 3.0, 4)
        db._kick_rebuild(*args)
        assert len(starts) == 1, "正常情况应照常后台重建"

        db._rebuilding.clear()
        db._rebuild_done_at.clear()
        db._record_promote_fail(args[0])
        db._kick_rebuild(*args)
        assert len(starts) == 1, "退避期间不该重建（旧缓存继续服务）"
        assert db._promote_backoff_left(args[0]) > 0

    def test_backoff_expires_and_success_clears_it(self, monkeypatch):
        db = _fake_db()
        rel = "contact/contact.db"
        starts = []
        monkeypatch.setattr(dbmod.threading, "Thread",
                            lambda **kw: starts.append(kw) or _NoopThread())
        args = (rel, "src", "dst", b"k", 1.0, 2, 3.0, 4)
        db._record_promote_fail(rel)
        db._promote_fail_at[rel] = (time.monotonic()
                                    - dbmod.PROMOTE_FAIL_BACKOFF_SEC - 1)
        db._kick_rebuild(*args)
        assert len(starts) == 1, "退避到点必须恢复重建，否则缓存永远停在旧版"

        db._rebuild_done_at.clear()
        db._rebuilding.clear()
        db._record_promote_fail(rel)
        assert db._promote_backoff_left(rel) > 0
        db._record_cache_ok(rel)
        assert db._promote_backoff_left(rel) == 0, "晋升成功即解除退避"

    def test_degraded_promote_failure_does_not_write_stamp(self, monkeypatch,
                                                            tmp_path):
        """旧实现晋升失败也盖 stamp：缓存停在旧数据却记为「已追平」→ 静默陈旧。"""
        rel = "contact__contact.db"   # dst = workdir/rel（无分隔符时原样）
        src = _make_db(str(tmp_path / "contact.db"), "SRC")
        dst = str(tmp_path / "contact__contact.db")
        _make_db(dst, "OLD")
        db = _fake_db()
        db._keys = {rel: b"k"}
        db._db_files = [(rel, src, os.path.getsize(src))]
        db.workdir = str(tmp_path)
        monkeypatch.setattr(WeChatDB, "_build_cache",
                            lambda self, s, t, d, k, stats=None: (b"", "full"))
        monkeypatch.setattr(WeChatDB, "_merge_wal",
                            lambda self, *a, **k: (0, 0))
        monkeypatch.setattr(WeChatDB, "_check_merged", lambda self, p: True)
        monkeypatch.setattr(WeChatDB, "_promote_cache", lambda self, t, d: False)
        assert db._open(rel) is not None          # 旧缓存仍可应答
        assert _rows(dst) == [("OLD",)]
        assert not os.path.exists(dst + ".stamp"), "晋升失败不得落 stamp"
        assert db._promote_backoff_left(rel) > 0, "并进入退避"


class _NoopThread:
    def __init__(self):
        self.name = ""

    def start(self):
        pass


# ---------------------------------------------------------------------------
# 7. 真实 __init__ 必须带齐缓存健康度字段（1.4.3 现场回归）
# ---------------------------------------------------------------------------
def _cache_state_fields():
    """_ensure_cache_state() 负责铺的字段名集合（未来加字段自动纳入断言）。"""
    probe = object.__new__(WeChatDB)
    WeChatDB._ensure_cache_state(probe)
    return {name for name in vars(probe) if name != "_cache_state_ready"}


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    """走真实 __init__ 的实例：只替换账号探测/扫库/取密钥三件外部依赖。"""
    db_dir = tmp_path / "db"
    (db_dir / "acct").mkdir(parents=True)
    monkeypatch.setattr(dbmod, "auto_detect_db_dir", lambda *a, **k: str(db_dir))
    monkeypatch.setattr(WeChatDB, "_pick_account", lambda self: "acct")
    monkeypatch.setattr(WeChatDB, "_collect_db_files", lambda self: [])
    monkeypatch.setattr(WeChatDB, "_load_or_extract_keys", lambda self, **k: None)
    return WeChatDB(db_dir=str(db_dir), workdir=str(tmp_path / "work"),
                    keys_file=str(tmp_path / "keys.json"))


class TestRealInitCacheState:
    def test_init_populates_every_field_ensure_cache_state_owns(self, real_db):
        """__init__ 与 _ensure_cache_state 只能有一份字段清单。

        1.4.3 现场：__init__ 自己铺这些字典并直接置 _cache_state_ready=True，
        新加的 _promote_fail_at 只写进了 _ensure_cache_state() 的惰性分支，
        于是真实实例永远没有它（/api/status 报 AttributeError、缓存健康面板
        空白），而用 object.__new__ 构造的用例照样全绿。
        """
        missing = _cache_state_fields() - set(vars(real_db))
        assert not missing, "真实实例缺字段 %s：__init__ 不会再铺字典，" \
                            "新字段加进 _ensure_cache_state() 即可" % missing

    def test_health_and_rebuild_paths_survive_real_instance(self, real_db,
                                                            monkeypatch):
        """cache_status/_kick_rebuild/_record_* 在真实实例上不得抛异常。

        _kick_rebuild() 第一行就是退避查询，它一抛 AttributeError 就沿着
        _open() 冒到 poller 的 `except Exception: continue` 被吞掉：既不重建
        也不报错（实测 1.4.3 升级后 promote 全停，缓存停在 11:43:27，
        之后新发的消息读不到）。
        """
        rel = "message\\message_8.db"
        starts = []
        monkeypatch.setattr(dbmod.threading, "Thread",
                            lambda **kw: starts.append(kw) or _NoopThread())
        assert real_db.cache_status() == []
        real_db._record_promote_fail(rel)
        assert real_db._promote_backoff_left(rel) > 0
        items = real_db.cache_status()
        assert [i["rel"] for i in items] == [rel]
        assert items[0]["promote_backoff"] > 0
        real_db._record_cache_ok(rel)
        assert real_db._promote_backoff_left(rel) == 0
        real_db._kick_rebuild(rel, "src", "dst", b"k", 0.0, 0, 0.0, 0)
        assert len(starts) == 1, "真实实例必须能正常触发后台重建"
