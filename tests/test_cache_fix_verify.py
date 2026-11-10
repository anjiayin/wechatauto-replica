# -*- coding: utf-8 -*-
"""QA 独立验证：解密缓存重建停摆修复（A-G）。

与 ``tests/test_db_cache.py``（工程师自测）刻意解耦：本文件不复用其用例与
断言，全部用临时伪页 + 替身重新构造，并额外覆盖工程师测试未涉及的边界：

- A 失败可见性：构建异常 / 校验失败 / 瞬时失败后恢复；
- B 全局配额：模块级 ``Semaphore`` 跨实例的真实并行上限、跳过不卡死；
- C 快速重试：源库持续改写时只在最后一轮付整库校验；``_source_sig``
  对 mtime / size / WAL salt / WAL 帧数的敏感性；
- D 陈旧告警：120s 阈值、60s 去重、时钟回拨、从未成功；
- E ``stats`` 参数不改变 ``增量 N 页`` 语义（四个历史场景双跑对比）；
- G 异常路径的配额归还与状态清理（不泄漏、不死锁）。

不读取真实微信库，不触碰 8765 服务。
"""

import contextlib
import io
import os
import shutil
import struct
import tempfile
import threading
import time
import unittest
from datetime import datetime
from unittest.mock import patch

from wechatauto import db as db_module
from wechatauto.db import (
    BG_REBUILD_ATTEMPTS,
    CACHE_STALE_WARN_INTERVAL_SEC,
    CACHE_STALE_WARN_SEC,
    FAST_RETRY_INTERVAL_SEC,
    MAX_CONCURRENT_REBUILDS,
    PAGE_SZ,
    WeChatDB,
)

REL = "message/message_8.db"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _write_fake_source(path, logical_pages=2, preallocated_pages=3):
    """写一个只有页头的伪 SQLCipher 库（页号填充，便于比对增量）。"""
    page1 = bytearray(PAGE_SZ)
    page1[:16] = b"SQLite format 3\x00"
    page1[28:32] = struct.pack(">I", logical_pages)
    with open(path, "wb") as src:
        src.write(page1)
        for pgno in range(2, logical_pages + 1):
            src.write(bytes([pgno]) * PAGE_SZ)
        src.write(b"P" * PAGE_SZ * preallocated_pages)


def _build_wal(path, frames, salt=b"testsalt"):
    """写一个含 ``frames`` 个 page1 提交帧的 WAL（帧大小与真实一致）。"""
    page1 = bytearray(PAGE_SZ)
    page1[:16] = b"SQLite format 3\x00"
    page1[28:32] = struct.pack(">I", 2)
    header = b"".join((
        struct.pack(">IIII", 0x377F0682, 3007000, PAGE_SZ, 0),
        salt, b"\x00" * 8,
    ))
    with open(path, "wb") as wal:
        wal.write(header)
        for _ in range(frames):
            wal.write(struct.pack(">II", 1, 2))
            wal.write(salt)
            wal.write(b"\x00" * 8)
            wal.write(page1)


class _Base(unittest.TestCase):
    """构造一个只有后台重建所需字段的 WeChatDB（兼容 __new__ 构造）。"""

    def setUp(self):
        # 用 mkdtemp + rmtree(ignore_errors) 而不是 TemporaryDirectory()：
        # Windows 上若仍有后台线程/未关闭句柄指向目录内文件，cleanup 会抛
        # PermissionError，把一个"通过"的用例变成 ERROR（实测过）。
        self._root = tempfile.mkdtemp(prefix="kefu-cache-test-")
        self.addCleanup(shutil.rmtree, self._root, True)

    def wait_until(self, predicate, timeout=30.0, interval=0.01):
        """轮询等待条件成立（确定性等待，替代固定 sleep 猜时序）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return bool(predicate())

    def join_all(self, threads, timeout=30.0):
        """join 全部线程并返回是否全部退出（不抛异常，便于 finally 调用）。"""
        deadline = time.monotonic() + timeout
        for t in threads:
            remain = max(0.0, deadline - time.monotonic())
            t.join(timeout=remain)
        return not any(t.is_alive() for t in threads)

    # -- 实例 -----------------------------------------------------------
    def make_db(self, use_global_sem=True, subdir="a"):
        db = WeChatDB.__new__(WeChatDB)
        db._rebuild_guard = threading.Lock()
        db._rebuild_locks = {}
        db._rebuilding = set()
        db._rebuild_done_at = {}
        if not use_global_sem:
            # 独立配额：隔离用例，避免污染进程内全局 Semaphore
            db._rebuild_sem = threading.Semaphore(MAX_CONCURRENT_REBUILDS)
        db._ensure_cache_state()
        return db

    def paths(self, subdir="a", name="message_8.db"):
        root = os.path.join(self._root, subdir)
        os.makedirs(root, exist_ok=True)
        return os.path.join(root, name), os.path.join(root, "cache.db")

    # -- 执行 -----------------------------------------------------------
    def run_bg(self, db, rel, src, dst, patch_decrypt=True):
        """同步跑一轮后台重建，返回捕获的 stderr 文本。"""
        buf = io.StringIO()
        patches = [patch.object(db, "_wal_path", return_value=None)]
        if patch_decrypt:
            patches.append(patch("wechatauto.db._decrypt_page",
                                 side_effect=lambda key, page, pgno: page))
        try:
            for p in patches:
                p.start()
            with contextlib.redirect_stderr(buf):
                db._bg_rebuild(rel, src, dst, b"key",
                               os.path.getmtime(src),
                               os.path.getsize(src), 0.0, 0)
        finally:
            for p in patches:
                p.stop()
        return buf.getvalue()

    def warn_stale(self, db, rel):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            db._maybe_warn_stale(rel)
        return buf.getvalue()

    def status_of(self, db, rel):
        return {item["rel"]: item for item in db.cache_status()}.get(rel, None)


# ---------------------------------------------------------------------------
# A. 失败可见性：不再静默 return
# ---------------------------------------------------------------------------
class FailureVisibilityTest(_Base):

    def test_build_exception_three_times_is_logged_with_shard_and_attempts(self):
        """构建连续 3 次抛异常 → stderr 必含分片名、尝试次数、最后错误。"""
        db = self.make_db(use_global_sem=False)
        src, dst = self.paths()
        _write_fake_source(src)

        with patch.object(db, "_build_cache", side_effect=OSError("源库被独占")):
            started = time.monotonic()
            out = self.run_bg(db, REL, src, dst, patch_decrypt=False)
            cost = time.monotonic() - started

        self.assertIn("缓存重建失败", out, "三次失败必须留痕，不能静默 return")
        self.assertIn(REL, out, "日志必须带分片名")
        self.assertIn("本轮尝试 %d 次" % BG_REBUILD_ATTEMPTS, out)
        self.assertIn("源库被独占", out, "日志必须带最后错误")
        self.assertIn("连续 1 次", out, "日志必须带连续失败计数")
        # 快重试：两次退避应为 0.2s，而不是原来逐轮 1.0s/1.5s
        self.assertLess(cost, 1.5, "失败重试应按 FAST_RETRY 间隔而非撕裂退避")
        self.assertFalse(os.path.exists(dst), "失败不得晋升坏缓存")
        self.assertFalse(os.path.exists(dst + ".rebuild"), "临时文件应清理")
        item = self.status_of(db, REL)
        self.assertEqual(item["fail_streak"], 1)
        self.assertIn("源库被独占", item["last_error"])
        self.assertIsNone(item["stale_seconds"])

    def test_check_failure_three_times_is_logged(self):
        """quick_check 连续 3 次失败 → 日志含分片名/尝试次数/校验原因。"""
        db = self.make_db(use_global_sem=False)
        src, dst = self.paths()
        _write_fake_source(src)

        with patch.object(db, "_build_cache", return_value=(b"", "全量")), \
                patch.object(db, "_check_merged", return_value=False):
            out = self.run_bg(db, REL, src, dst, patch_decrypt=False)

        self.assertIn("缓存重建失败", out)
        self.assertIn(REL, out)
        self.assertIn("本轮尝试 %d 次" % BG_REBUILD_ATTEMPTS, out)
        self.assertIn("quick_check 校验失败", out)
        self.assertIn("连续 1 次", out)
        self.assertFalse(os.path.exists(dst))
        self.assertFalse(os.path.exists(dst + ".rebuild"))

    def test_transient_failure_recovers_without_poisoning_state(self):
        """首轮构建失败、次轮成功 → 不报失败、失败计数为 0。"""
        db = self.make_db(use_global_sem=False)
        src, dst = self.paths()
        _write_fake_source(src)
        # 先建好正式缓存与 .pages 边车，让第二轮走增量
        with patch("wechatauto.db._decrypt_page",
                   side_effect=lambda key, page, pgno: page):
            hashes, _how = db._build_cache(src, dst + ".tmp", dst, b"key")
        with open(dst + ".tmp.pages", "wb") as fh:
            fh.write(hashes)
        self.assertTrue(WeChatDB._promote_cache(dst + ".tmp", dst))

        real_build = db._build_cache
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("瞬时失败")
            return real_build(*args, **kwargs)

        with patch.object(db, "_build_cache", side_effect=flaky), \
                patch("wechatauto.db._decrypt_page",
                      side_effect=lambda key, page, pgno: page):
            out = self.run_bg(db, REL, src, dst)

        self.assertNotIn("缓存重建失败", out)
        self.assertIn("缓存重建完成", out)
        item = self.status_of(db, REL)
        self.assertEqual(item["fail_streak"], 0)
        self.assertEqual(item["last_error"], "")

    def test_failure_log_contains_elapsed_and_retry_hint(self):
        """失败日志需给出耗时与下次重试间隔（排障所需的最小信息集）。"""
        db = self.make_db(use_global_sem=False)
        src, dst = self.paths()
        _write_fake_source(src)
        with patch.object(db, "_build_cache", side_effect=OSError("boom")):
            out = self.run_bg(db, REL, src, dst, patch_decrypt=False)
        self.assertRegex(out, r"耗时 \d+\.\d+s")
        self.assertRegex(out, r"\d+s 后重试")


# ---------------------------------------------------------------------------
# B. 全局并发配额
# ---------------------------------------------------------------------------
class ConcurrencyQuotaTest(_Base):

    def _restore_global_sem(self):
        sem = db_module._REBUILD_SEMAPHORE
        while sem._value < MAX_CONCURRENT_REBUILDS:  # 归还泄漏的配额
            sem.release()

    def test_parallel_rebuilds_never_exceed_global_quota(self):
        """4 个分片同时重建，实际并行数不超过 2；被跳过的后续仍能推进。"""
        self.addCleanup(self._restore_global_sem)
        rels = ["message/message_%d.db" % i for i in range(4)]
        dbs, srcs, dsts = {}, {}, {}
        for rel in rels:
            sub = rel.replace("/", "_")
            db = self.make_db(use_global_sem=True, subdir=sub)
            # 生产同源：所有实例共享模块级 Semaphore
            self.assertIs(db._rebuild_sem, db_module._REBUILD_SEMAPHORE)
            src, dst = self.paths(subdir=sub)
            _write_fake_source(src)
            dbs[rel], srcs[rel], dsts[rel] = db, src, dst

        lock = threading.Lock()
        active = {"now": 0, "peak": 0}

        def slow_build(self, src, tmp, dst, key, stats=None):
            with lock:
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
            try:
                time.sleep(0.35)
                with open(tmp, "wb") as fh:
                    fh.write(b"cached")
                if stats is not None:
                    stats.update(full=False, patched=0)
                return b"\x00" * 8, "增量 0 页"
            finally:
                with lock:
                    active["now"] -= 1

        errors = []

        def worker(rel):
            try:
                dbs[rel]._bg_rebuild(rel, srcs[rel], dsts[rel], b"key", 0.0, 0, 0.0, 0)
            except Exception as exc:  # noqa: BLE001 - 记录到主线程断言
                errors.append((rel, exc))

        with patch.object(WeChatDB, "_build_cache", slow_build), \
                patch.object(WeChatDB, "_wal_path", return_value=None):
            threads = [threading.Thread(target=worker, args=(rel,)) for rel in rels]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

        self.assertFalse(errors, "重建线程不应抛异常：%s" % (errors,))
        self.assertFalse(any(t.is_alive() for t in threads), "不得死锁/永久阻塞")
        self.assertLessEqual(active["peak"], MAX_CONCURRENT_REBUILDS,
                             "同时重建分片数超过全局配额")
        self.assertEqual(db_module._REBUILD_SEMAPHORE._value,
                         MAX_CONCURRENT_REBUILDS, "配额未归还（泄漏会导致永久停摆）")
        # 被跳过的分片必须已清除 rebuilding 标记，能排出 cache_status
        for rel in rels:
            self.assertNotIn(rel, dbs[rel]._rebuilding)
            for item in dbs[rel].cache_status():
                self.assertFalse(item["rebuilding"],
                                 "被跳过的分片被永久标记为 rebuilding")

        built = [rel for rel in rels if os.path.exists(dsts[rel])]
        skipped = [rel for rel in rels if rel not in built]
        self.assertGreaterEqual(len(skipped), 1,
                                "4 个分片并发、配额 2 时应至少有一个被跳过")

        # 后续推进：被跳过的分片下一轮可以正常重建
        for rel in skipped:
            with patch.object(dbs[rel], "_check_merged", return_value=True), \
                    patch("wechatauto.db._decrypt_page",
                          side_effect=lambda key, page, pgno: page):
                out = self.run_bg(dbs[rel], rel, srcs[rel], dsts[rel])
            self.assertIn("缓存重建完成", out)
            self.assertTrue(os.path.exists(dsts[rel]), "跳过的分片未能推进重建")


# ---------------------------------------------------------------------------
# C. 快速重试与签名
# ---------------------------------------------------------------------------
class FastRetryTest(_Base):

    def test_source_still_writing_skips_all_but_last_check(self):
        """源库每轮都在变：只在最后一轮付整库校验，且最终仍晋升出快照。"""
        db = self.make_db(use_global_sem=False)
        src, dst = self.paths()
        _write_fake_source(src)
        checks = []
        real_build = db._build_cache

        def growing_build(*args, **kwargs):
            with open(src, "ab") as fh:  # 模拟微信在构建期间继续写库
                fh.write(b"X" * PAGE_SZ)
            return real_build(*args, **kwargs)

        def counting_check(path):
            checks.append(path)
            return True

        with patch.object(db, "_build_cache", side_effect=growing_build), \
                patch.object(db, "_check_merged", side_effect=counting_check), \
                patch("wechatauto.db._decrypt_page",
                      side_effect=lambda key, page, pgno: page):
            started = time.monotonic()
            out = self.run_bg(db, REL, src, dst)
            cost = time.monotonic() - started

        self.assertEqual(len(checks), 1,
                         "源库持续改写时不应每轮都做整库 quick_check")
        self.assertIn("缓存重建完成", out)
        self.assertNotIn("缓存重建失败", out)
        self.assertLess(cost, BG_REBUILD_ATTEMPTS * 0.9,
                        "应按 FAST_RETRY 间隔重试，而非逐轮 1s 退避")
        self.assertTrue(os.path.exists(dst), "最后一轮仍须推进出可用快照")
        self.assertLessEqual(FAST_RETRY_INTERVAL_SEC, 0.5)

    def test_source_signature_detects_each_component_change(self):
        """签名必须对 mtime / size / WAL salt / WAL 帧数 各自敏感。"""
        db = self.make_db(use_global_sem=False)
        src, dst = self.paths()
        wal = src + "-wal"
        _write_fake_source(src)
        _build_wal(wal, frames=2)
        os.utime(src, (1_700_000_000, 1_700_000_000))
        os.utime(wal, (1_700_000_000, 1_700_000_000))

        base = db._source_sig(src, wal)
        self.assertEqual(len(base), 4)
        self.assertEqual(base, db._source_sig(src, wal), "无变化时签名必须稳定")
        self.assertEqual(base[3], 2, "帧数应为 (size-header)//frame_sz")

        # mtime 变化
        os.utime(src, (1_700_000_010, 1_700_000_010))
        self.assertNotEqual(db._source_sig(src, wal), base)
        os.utime(src, (1_700_000_000, 1_700_000_000))

        # WAL salt 变化（checkpoint 重置世代的真实信号）
        _build_wal(wal, frames=2, salt=b"othersalt")
        os.utime(wal, (1_700_000_000, 1_700_000_000))
        salt_sig = db._source_sig(src, wal)
        self.assertNotEqual(salt_sig, base, "WAL salt 变化必须改变签名")

        # WAL 帧数变化
        _build_wal(wal, frames=3, salt=b"othersalt")
        os.utime(wal, (1_700_000_000, 1_700_000_000))
        self.assertNotEqual(db._source_sig(src, wal), salt_sig,
                            "WAL 帧数变化必须改变签名")

        # 主库 size 变化
        with open(src, "ab") as fh:
            fh.write(b"Y" * PAGE_SZ)
        self.assertNotEqual(db._source_sig(src, wal), salt_sig)

    def test_source_signature_survives_missing_files(self):
        """源库/WAL 不存在时签名不得抛异常（否则后台线程会静默死掉）。"""
        db = self.make_db(use_global_sem=False)
        missing = os.path.join(self._root, "nope.db")
        self.assertEqual(db._source_sig(missing, None), (0.0, -1, b"", 0))
        self.assertEqual(db._source_sig(missing, missing + "-wal"),
                         (0.0, -1, b"", 0))


# ---------------------------------------------------------------------------
# D. 陈旧告警
# ---------------------------------------------------------------------------
class StaleWarningTest(_Base):

    def test_never_succeeded_reports_none_but_warns_after_threshold(self):
        """从未成功：stale_seconds=None；超过 120s 后必须告警且带失败信息。"""
        db = self.make_db(use_global_sem=False)
        item = self.status_of_after_error(db)
        self.assertIsNone(item["stale_seconds"])
        self.assertEqual(item["last_ok_at"], "")
        self.assertEqual(item["fail_streak"], 1)

    def status_of_after_error(self, db):
        db._record_cache_error(REL, "构建异常 OSError: 磁盘忙")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            db._maybe_warn_stale(REL)  # 首次调用仅登记计时，不应告警
        self.assertEqual(buf.getvalue(), "", "刚启动不应误报陈旧")

        db._cache_first_seen[REL] = time.time() - (CACHE_STALE_WARN_SEC + 5)
        first = self.warn_stale(db, REL)
        self.assertIn("WARNING", first)
        self.assertIn(REL, first)
        self.assertIn("磁盘忙", first, "告警应带最后错误，便于定位")
        self.assertIn("连续失败 1 次", first)
        self.assertEqual(self.warn_stale(db, REL), "", "60s 内不得重复刷屏")

        # 去重窗口过期后允许再次告警
        db._stale_warned_at[REL] = time.time() - (CACHE_STALE_WARN_INTERVAL_SEC + 1)
        self.assertIn("WARNING", self.warn_stale(db, REL), "窗口过后应再次告警")
        return self.status_of(db, REL)

    def test_threshold_boundary_119s_quiet_121s_warns(self):
        """120s 阈值边界：119s 静默、121s 告警。"""
        db = self.make_db(use_global_sem=False)
        db._record_cache_ok(REL)

        db._cache_ok_at[REL] = time.time() - (CACHE_STALE_WARN_SEC - 1)
        self.assertEqual(self.warn_stale(db, REL), "", "未到阈值不得告警")

        db._cache_ok_at[REL] = time.time() - (CACHE_STALE_WARN_SEC + 1)
        self.assertIn("WARNING", self.warn_stale(db, REL))

    def test_stale_seconds_clamps_clock_skew_and_fresh_is_small(self):
        """时钟回拨（ok_at 在未来）时 stale_seconds 不得为负。"""
        db = self.make_db(use_global_sem=False)
        db._record_cache_ok(REL)
        fresh = self.status_of(db, REL)
        self.assertLess(fresh["stale_seconds"], 1.0)
        self.assertNotEqual(fresh["last_ok_at"], "")
        datetime.strptime(fresh["last_ok_at"], "%Y-%m-%dT%H:%M:%S")  # 格式可解析

        db._cache_ok_at[REL] = time.time() + 120
        self.assertEqual(self.status_of(db, REL)["stale_seconds"], 0.0,
                         "时钟回拨时不得出现负的 stale_seconds")

    def test_warning_only_when_rebuild_needed(self):
        """缓存新鲜时即使调用告警检查也不输出。"""
        db = self.make_db(use_global_sem=False)
        db._record_cache_ok(REL)
        self.assertEqual(self.warn_stale(db, REL), "")


# ---------------------------------------------------------------------------
# E. stats 参数不得改变 "增量 N 页" 语义
# ---------------------------------------------------------------------------
class StatsSemanticsTest(_Base):
    """四个历史场景双跑（带/不带 stats），对比 how 字符串与页数。"""

    def _scenario(self, subdir, with_stats):
        db = self.make_db(use_global_sem=False, subdir=subdir)
        src, dst = self.paths(subdir=subdir)
        tmp = dst + ".rebuild"
        _write_fake_source(src, logical_pages=2, preallocated_pages=3)
        results = []

        def build():
            stats = {} if with_stats else None
            hashes, how = db._build_cache(src, tmp, dst, b"key", stats=stats)
            with open(tmp + ".pages", "wb") as fh:
                fh.write(hashes)
            self.assertTrue(WeChatDB._promote_cache(tmp, dst))
            results.append((how, None if stats is None else
                            (stats.get("full"), stats.get("patched"))))

        with patch("wechatauto.db._decrypt_page",
                   side_effect=lambda key, page, pgno: page):
            hashes = []
            db._decrypt_file(src, dst, b"key", hashes_out=hashes)
            with open(dst + ".pages", "wb") as fh:
                fh.write(b"".join(hashes))

            build()                                    # 1) 无变化
            with open(src, "r+b") as fh:               # 2) 数据页变更
                fh.seek(PAGE_SZ)
                fh.write(b"Z" * PAGE_SZ)
            build()
            with open(src, "r+b") as fh:               # 3) 逻辑扩容
                fh.seek(28)
                fh.write(struct.pack(">I", 4))
            build()

        return results

    def test_stats_does_not_change_incremental_semantics(self):
        plain = self._scenario("plain", with_stats=False)
        with_stats = self._scenario("stats", with_stats=True)

        self.assertEqual([r[0] for r in plain],
                         [r[0] for r in with_stats],
                         "stats 参数改变了 '增量 N 页' 语义")
        self.assertEqual([r[0] for r in plain],
                         ["增量 0 页", "增量 1 页", "增量 3 页"])
        # stats 回填必须与 how 字符串一致
        self.assertEqual(with_stats[0][1], (False, 0))
        self.assertEqual(with_stats[1][1], (False, 1))
        self.assertEqual(with_stats[2][1], (False, 3))

    def test_wal_overlaid_page1_scenario_unchanged(self):
        """WAL 覆盖 page1 后重建主库：带 stats 与不带 stats 结果一致（3 页）。"""
        hows = {}
        for with_stats in (False, True):
            sub = "wal_stats" if with_stats else "wal_plain"
            db = self.make_db(use_global_sem=False, subdir=sub)
            src, dst = self.paths(subdir=sub)
            tmp = dst + ".rebuild"
            _write_fake_source(src, logical_pages=4, preallocated_pages=1)
            with patch("wechatauto.db._decrypt_page",
                       side_effect=lambda key, page, pgno: page):
                hashes = []
                db._decrypt_file(src, dst, b"key", hashes_out=hashes)
                with open(dst + ".pages", "wb") as fh:
                    fh.write(b"".join(hashes))

                wal = src + "-wal"
                _build_wal(wal, frames=1)
                self.assertEqual(db._merge_wal(dst, wal, b"key", 0), (1, 1))
                self.assertEqual(os.path.getsize(dst), 2 * PAGE_SZ)

                stats = {} if with_stats else None
                new_hashes, how = db._build_cache(src, tmp, dst, b"key",
                                                  stats=stats)
                with open(tmp + ".pages", "wb") as fh:
                    fh.write(new_hashes)
                self.assertTrue(WeChatDB._promote_cache(tmp, dst))
                hows[with_stats] = how
                if with_stats:
                    self.assertEqual(stats, {"full": False, "patched": 3})
                self.assertEqual(os.path.getsize(dst), 4 * PAGE_SZ)

                # 恢复后再次重建应收敛为 0 页（不反复解密缺失页）
                stats2 = {}
                new_hashes2, how2 = db._build_cache(src, tmp, dst, b"key",
                                                    stats=stats2)
                self.assertEqual(how2, "增量 0 页")
                self.assertEqual(stats2, {"full": False, "patched": 0})

        self.assertEqual(hows[False], hows[True], "stats 改变了 WAL 场景的页数语义")
        self.assertEqual(hows[True], "增量 3 页")

    def test_unchanged_rebuild_decrypts_page1_exactly_once(self):
        """相同源库的增量重建只解密 page1 一次（不重复解密预分配尾页）。"""
        db = self.make_db(use_global_sem=False, subdir="once")
        src, dst = self.paths(subdir="once")
        tmp = dst + ".rebuild"
        _write_fake_source(src)
        with patch("wechatauto.db._decrypt_page",
                   side_effect=lambda key, page, pgno: page) as decrypt:
            hashes = []
            db._decrypt_file(src, dst, b"key", hashes_out=hashes)
            with open(dst + ".pages", "wb") as fh:
                fh.write(b"".join(hashes))
            for _ in range(3):
                decrypt.reset_mock()
                stats = {}
                new_hashes, how = db._build_cache(src, tmp, dst, b"key",
                                                  stats=stats)
                with open(tmp + ".pages", "wb") as fh:
                    fh.write(new_hashes)
                self.assertTrue(WeChatDB._promote_cache(tmp, dst))
                self.assertEqual(how, "增量 0 页")
                self.assertEqual(stats, {"full": False, "patched": 0})
                self.assertEqual(decrypt.call_count, 1,
                                 "每轮只应解密 page1（判断逻辑页数 / 清 WAL overlay）")
                self.assertEqual(decrypt.call_args.args[2], 1)

    def test_full_path_stats_marks_full_and_negative_patched(self):
        """全量路径 stats.full=True、patched=-1（调用方据此禁止跳过校验）。"""
        db = self.make_db(use_global_sem=False, subdir="full")
        src, dst = self.paths(subdir="full")
        _write_fake_source(src)
        stats = {}
        with patch("wechatauto.db._decrypt_page",
                   side_effect=lambda key, page, pgno: page):
            _hashes, how = db._build_cache(src, dst + ".rebuild", dst, b"key",
                                           stats=stats)
        self.assertEqual(how, "全量")
        self.assertEqual(stats, {"full": True, "patched": -1})


# ---------------------------------------------------------------------------
# G. 配额/锁 的异常路径
# ---------------------------------------------------------------------------
class LockAndQuotaSafetyTest(_Base):

    def test_unexpected_exception_releases_quota_and_state(self):
        """未预期异常必须被兜底捕获（R3）并留痕：配额归还、rebuilding 清除。

        R3 起 ``_bg_rebuild`` 的兜底捕获从 ``(OSError, sqlite3.Error)``
        扩到 ``Exception``：未知异常不再逃出线程静默杀死重建（这正是
        线上"3 小时无新商机"的形态），而是记入 fail_streak + stderr，
        并在三轮后正常收尾。原用例断言的"异常必须抛出"因此改为
        "异常必须被收容且状态必须归位"。
        """
        db = self.make_db(use_global_sem=False)
        src, dst = self.paths()
        _write_fake_source(src)
        sem = db._rebuild_sem
        db._rebuilding.add(REL)

        with patch.object(db, "_build_cache",
                          side_effect=RuntimeError("异常类型不在原捕获列表")):
            out = self.run_bg(db, REL, src, dst, patch_decrypt=False)

        self.assertIn("缓存重建失败", out, "未预期异常同样必须留痕")
        self.assertIn("RuntimeError", out, "日志必须带异常类型")
        self.assertEqual(sem._value, MAX_CONCURRENT_REBUILDS,
                         "异常路径未归还重建配额 → 后续分片永久停摆")
        self.assertNotIn(REL, db._rebuilding, "异常路径未清除 rebuilding 标记")
        self.assertIn(REL, db._rebuild_done_at, "异常路径未记录完成时刻")
        item = self.status_of(db, REL)
        self.assertEqual(item["fail_streak"], 1, "未预期异常也必须计入连续失败")
        self.assertIn("RuntimeError", item["last_error"])

    def test_same_shard_concurrent_rebuild_does_not_deadlock(self):
        """同一分片两个线程并发重建：串行化完成，不死锁、不泄漏配额。"""
        db = self.make_db(use_global_sem=False)
        src, dst = self.paths()
        _write_fake_source(src)
        real_build = db._build_cache
        calls = []

        def slow_build(_self, *args, **kwargs):
            calls.append(threading.current_thread().name)
            time.sleep(0.2)
            return real_build(*args, **kwargs)

        with patch.object(WeChatDB, "_build_cache", slow_build), \
                patch.object(WeChatDB, "_wal_path", return_value=None), \
                patch.object(WeChatDB, "_check_merged", return_value=True), \
                patch("wechatauto.db._decrypt_page",
                      side_effect=lambda key, page, pgno: page):
            threads = [threading.Thread(
                target=db._bg_rebuild,
                args=(REL, src, dst, b"key", 0.0, 0, 0.0, 0),
                name="rebuild-%d" % i) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        self.assertFalse(any(t.is_alive() for t in threads), "同分片并发重建死锁")
        self.assertEqual(len(calls), 2, "两个线程都应完成重建")
        self.assertEqual(db._rebuild_sem._value, MAX_CONCURRENT_REBUILDS)
        self.assertNotIn(REL, db._rebuilding)

    def test_kick_rebuild_recovers_after_quota_skip(self):
        """配额不足被跳过后，_kick_rebuild 仍能在下一轮调度成功（不永久卡住）。"""
        db = self.make_db(use_global_sem=False)
        src, dst = self.paths()
        _write_fake_source(src)
        sem = db._rebuild_sem
        sem.acquire()
        sem.acquire()  # 配额占满

        def wait_idle(timeout=5.0):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                with db._rebuild_guard:
                    if REL not in db._rebuilding:
                        return True
                time.sleep(0.02)
            return False

        buf = io.StringIO()
        with patch.object(db, "_wal_path", return_value=None), \
                contextlib.redirect_stderr(buf):
            db._kick_rebuild(REL, src, dst, b"key", 0.0, 0, 0.0, 0)
            self.assertTrue(wait_idle(), "跳过路径未清除 rebuilding 标记")
        self.assertIn("等待重建配额", buf.getvalue())

        sem.release()
        sem.release()
        with patch.object(db, "_wal_path", return_value=None), \
                patch.object(db, "_check_merged", return_value=True), \
                patch("wechatauto.db._decrypt_page",
                      side_effect=lambda key, page, pgno: page):
            db._kick_rebuild(REL, src, dst, b"key", 0.0, 0, 0.0, 0)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not os.path.exists(dst):
                time.sleep(0.02)
            self.assertTrue(wait_idle(), "第二轮 rebuild 未正常结束")
        self.assertTrue(os.path.exists(dst), "配额恢复后分片仍无法推进重建")


# ---------------------------------------------------------------------------
# R3/R4 复核（工程师在 QA 报告后追加的兜底捕获与同步重建可见性）
# ---------------------------------------------------------------------------
class ExceptionContainmentTest(_Base):
    """R3：未知异常必须被收容、只记一次失败、线程不再静默死亡。"""

    def test_unexpected_exception_is_contained_once_per_round(self):
        """未知异常：每轮 fail_streak 只 +1（3 次尝试不放大 3 倍），且逐次留痕。"""
        db = self.make_db(use_global_sem=False)
        src, dst = self.paths()
        _write_fake_source(src)

        with patch.object(db, "_build_cache",
                          side_effect=TypeError("key 类型错误")):
            out = self.run_bg(db, REL, src, dst, patch_decrypt=False)

        # 每次尝试都留痕（3 条），并在收尾给一条汇总
        self.assertEqual(out.count("缓存重建第 "), BG_REBUILD_ATTEMPTS)
        self.assertIn("第 1/%d 次尝试异常" % BG_REBUILD_ATTEMPTS, out)
        self.assertIn("TypeError", out)
        self.assertIn("缓存重建失败", out)
        item = self.status_of(db, REL)
        self.assertEqual(item["fail_streak"], 1,
                         "一轮 3 次尝试只应记 1 次失败，否则 fail_streak 被放大")
        self.assertIn("TypeError", item["last_error"])
        self.assertEqual(db._rebuild_sem._value, MAX_CONCURRENT_REBUILDS)
        self.assertNotIn(REL, db._rebuilding)
        self.assertFalse(os.path.exists(dst + ".rebuild"))

        # 第二轮再失败 → fail_streak 应为 2（跨轮累加，不是 2 倍放大）
        with patch.object(db, "_build_cache",
                          side_effect=TypeError("key 类型错误")):
            self.run_bg(db, REL, src, dst, patch_decrypt=False)
        self.assertEqual(self.status_of(db, REL)["fail_streak"], 2)

    def test_background_thread_no_longer_dies_silently(self):
        """未知异常不得逃出后台线程（逃出=线程死亡+无状态，正是本次故障形态）。"""
        db = self.make_db(use_global_sem=False)
        src, dst = self.paths()
        _write_fake_source(src)
        escaped = []
        original_hook = threading.excepthook

        def hook(args):
            escaped.append(args.exc_value)

        threading.excepthook = hook
        self.addCleanup(setattr, threading, "excepthook", original_hook)
        buf = io.StringIO()
        with patch.object(db, "_wal_path", return_value=None), \
                patch.object(db, "_build_cache",
                             side_effect=TypeError("未知异常类型")), \
                contextlib.redirect_stderr(buf):
            db._kick_rebuild(REL, src, dst, b"key",
                             os.path.getmtime(src), os.path.getsize(src), 0.0, 0)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with db._rebuild_guard:
                    if REL not in db._rebuilding:
                        break
                time.sleep(0.02)
        self.assertEqual(escaped, [], "未知异常仍逃出线程 → 重建会静默死亡")
        self.assertIn("TypeError", buf.getvalue())
        self.assertEqual(self.status_of(db, REL)["fail_streak"], 1)


class SyncRebuildVisibilityTest(_Base):
    """R4：同步重建（_open 阻塞路径）也要让 rebuilding 可见，且标记不踩踏。"""

    def _open_fixture(self, subdir):
        db = self.make_db(use_global_sem=False, subdir=subdir)
        src, _dst = self.paths(subdir=subdir)
        _write_fake_source(src)
        db.workdir = os.path.join(self._root, subdir)
        db._keys = {REL: b"key"}
        db.keys_file = os.path.join(db.workdir, "keys.json")
        db._db_path = lambda rel: src          # 实例属性覆盖，避免真实微信路径
        dst = os.path.join(db.workdir, REL.replace(os.sep, "__"))
        return db, src, dst

    @staticmethod
    def _fake_build(entered=None, release=None):
        def build(src, tmp, dst, key, stats=None):
            os.makedirs(os.path.dirname(tmp), exist_ok=True)
            with open(tmp, "wb") as fh:
                fh.write(b"x" * PAGE_SZ)
            if stats is not None:
                stats.update(full=False, patched=0)
            if entered is not None:
                entered.set()
            if release is not None:
                release.wait(5)
            return b"\x00" * 8, "增量 0 页"
        return build

    def test_sync_rebuild_visible_during_and_cleared_after(self):
        """同步重建期间 cache_status().rebuilding 必须为 True，结束后为 False。

        稳定性约定（曾间歇失败，勿回退）：
        - "重建中" 的采样放在 ``_build_cache`` 替身内部，即与 ``_syncing_cache``
          同一线程、已在标记覆盖范围内，**不靠外部 sleep 猜时序**；
        - 外部只轮询等待标记出现（``wait_until``，带 30s 上限）；
        - ``finally`` 里无条件放行并 join worker，且必须在 ``with patch(...)``
          内部完成——否则用例先失败会把仍然存活的 worker 留到 teardown，
          在 Windows 上抛出临时目录清理 ERROR（失败被放大成 failed+error）。
        """
        db, src, dst = self._open_fixture("sync_visible")
        entered, release = threading.Event(), threading.Event()
        result = {}
        observed = {}

        def build(src_path, tmp, dst_path, key, stats=None):
            # 此处必然已进入 _syncing_cache：确定性地观察标记
            item = self.status_of(db, REL)
            observed["during"] = None if item is None else item["rebuilding"]
            os.makedirs(os.path.dirname(tmp), exist_ok=True)
            with open(tmp, "wb") as fh:
                fh.write(b"x" * PAGE_SZ)
            if stats is not None:
                stats.update(full=False, patched=0)
            entered.set()
            release.wait(30)          # 由 finally 放行；超时仅作死锁兜底
            return b"\x00" * 8, "增量 0 页"

        def worker():
            try:
                conn = db._open(REL)
                result["ok"] = True
                conn.close()          # sqlite 连接只能在创建它的线程关闭
            except Exception as exc:  # noqa: BLE001
                result["error"] = exc

        t = threading.Thread(target=worker, name="sync-open", daemon=True)
        with patch.object(db, "_wal_path", return_value=None), \
                patch.object(db, "_read_stamp", return_value=None), \
                patch.object(db, "_check_merged", return_value=True), \
                patch.object(db, "_build_cache", build):
            t.start()
            try:
                self.assertTrue(
                    self.wait_until(lambda: (self.status_of(db, REL) or {})
                                    .get("rebuilding") is True),
                    "同步重建期间该分片未在 cache_status 中显示 rebuilding")
                self.assertTrue(entered.wait(30), "同步重建未进入构建阶段")
            finally:
                release.set()          # 断言失败也必须放行，避免线程泄漏到 teardown
                self.join_all([t])

        self.assertTrue(observed.get("during"), "重建内部观察到 rebuilding 必须为 True")
        self.assertFalse(t.is_alive(), "同步重建路径死锁")
        self.assertNotIn("error", result, "同步重建不应抛异常：%s" % result.get("error"))
        after = self.status_of(db, REL)
        self.assertIsNotNone(after)
        self.assertFalse(after["rebuilding"], "同步重建结束后必须清除 rebuilding")
        self.assertTrue(os.path.exists(dst))

    def test_sync_rebuild_does_not_clear_foreign_rebuilding_marker(self):
        """同步路径只清自己加的标记，不得踩掉后台线程的 rebuilding。"""
        db, src, dst = self._open_fixture("sync_foreign")
        db._rebuilding.add(REL)      # 模拟后台线程已持有标记
        try:
            with patch.object(db, "_wal_path", return_value=None), \
                    patch.object(db, "_read_stamp", return_value=None), \
                    patch.object(db, "_check_merged", return_value=True), \
                    patch.object(db, "_build_cache", self._fake_build()):
                conn = db._open(REL)
        finally:
            conn.close()
        self.assertIn(REL, db._rebuilding,
                      "同步路径踩掉了属于后台线程的 rebuilding 标记")

    def test_sync_rebuild_clears_marker_when_build_raises(self):
        """同步重建抛异常时也必须清除标记（否则该分片永远不再被调度）。"""
        db, src, dst = self._open_fixture("sync_raise")
        buf = io.StringIO()
        with patch.object(db, "_wal_path", return_value=None), \
                patch.object(db, "_read_stamp", return_value=None), \
                patch.object(db, "_build_cache", side_effect=OSError("磁盘忙")), \
                contextlib.redirect_stderr(buf):
            with self.assertRaises(OSError):
                db._open(REL)
        self.assertNotIn(REL, db._rebuilding, "异常路径泄漏 rebuilding 标记")
        self.assertIn("同步重建失败", buf.getvalue())
        self.assertEqual(self.status_of(db, REL)["fail_streak"], 1)

    def test_no_deadlock_between_sync_marker_and_kick(self):
        """同步重建期间并发 _kick_rebuild / cache_status 不得死锁或残留标记。

        同样遵守"finally 内无条件放行 + join，且必须在 patch 生效期间完成"，
        避免断言失败后残留线程干扰 teardown（Windows 上会被放大成 ERROR）。
        """
        db, src, dst = self._open_fixture("sync_race")
        entered, release = threading.Event(), threading.Event()
        samples = []
        stop = threading.Event()

        def sampler():
            while not stop.is_set():
                samples.extend((item["rel"], item["rebuilding"])
                               for item in db.cache_status())
                time.sleep(0.005)

        def kicker():
            for _ in range(20):
                db._kick_rebuild(REL, src, dst, b"key", 0.0, 0, 0.0, 0)
                time.sleep(0.01)

        def sync_open():
            conn = db._open(REL)
            conn.close()              # sqlite 连接只能在创建它的线程关闭

        threads = [threading.Thread(target=sync_open, name="sync-open", daemon=True),
                   threading.Thread(target=sampler, name="sampler", daemon=True),
                   threading.Thread(target=kicker, name="kicker", daemon=True)]
        with patch.object(db, "_wal_path", return_value=None), \
                patch.object(db, "_read_stamp", return_value=None), \
                patch.object(db, "_check_merged", return_value=True), \
                patch.object(db, "_build_cache",
                             self._fake_build(entered, release)):
            for t in threads:
                t.start()
            try:
                self.assertTrue(entered.wait(30), "同步重建未进入构建阶段")
                # 轮询替代固定 sleep：只需 sampler 采到至少一次 rebuilding=True
                self.assertTrue(
                    self.wait_until(lambda: any(flag for _, flag in samples), 10),
                    "重建期间 cache_status 未采到 rebuilding=True")
            finally:
                release.set()         # 先放行重建
                stop.set()            # 再停采样线程，避免 join 等满 timeout
                self.join_all(threads)
            # kicker 可能已派生出后台重建线程，等其自然收尾再判定"无残留"
            self.assertTrue(
                self.wait_until(lambda: REL not in db._rebuilding, 10),
                "竞争后残留 rebuilding 标记")

        self.assertFalse(any(t.is_alive() for t in threads), "同步/后台路径死锁")
        self.assertTrue(samples, "重建期间 cache_status 未被采样")
        self.assertNotIn(REL, db._rebuilding, "竞争后残留 rebuilding 标记")
        self.assertEqual(db._rebuild_sem._value, MAX_CONCURRENT_REBUILDS,
                         "竞争后配额泄漏")


if __name__ == "__main__":
    unittest.main()
