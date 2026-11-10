# -*- coding: utf-8 -*-
"""解密缓存页级增量重建与后台重建健壮性的回归测试。

覆盖两部分：
1. 页级增量重建（预分配尾页 / WAL overlay 修正）——历史修复；
2. 后台重建的失败可见性、快速重试、并发上限、陈旧告警——对应线上
   "14:39–17:52 三小时无新商机、17:52 后积压消息一次性涌入"故障。
"""

import contextlib
import io
import os
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from wechatauto.db import (
    CACHE_STALE_WARN_SEC,
    MAX_CACHE_ERROR_LEN,
    MAX_CONCURRENT_REBUILDS,
    PAGE_SZ,
    WeChatDB,
)


class CacheRebuildTest(unittest.TestCase):
    """使用临时伪页验证缓存行为，不依赖真实微信数据库。"""

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.src = os.path.join(self._tempdir.name, "src.db")
        self.dst = os.path.join(self._tempdir.name, "cache.db")
        self.tmp = self.dst + ".rebuild"
        self.db = WeChatDB.__new__(WeChatDB)
        # 后台重建需要的实例状态（正常由 __init__ 建立）
        self.db._rebuild_guard = threading.Lock()
        self.db._rebuild_locks = {}
        self.db._rebuilding = set()
        self.db._rebuild_done_at = {}
        # 每个用例独立的配额信号量，避免污染进程内全局配额
        self.db._rebuild_sem = threading.Semaphore(MAX_CONCURRENT_REBUILDS)
        self.db._ensure_cache_state()

    def _write_source(self, logical_pages: int = 2,
                      preallocated_pages: int = 3) -> None:
        page1 = bytearray(PAGE_SZ)
        page1[:16] = b"SQLite format 3\x00"
        page1[28:32] = struct.pack(">I", logical_pages)
        with open(self.src, "wb") as src:
            src.write(page1)
            for pgno in range(2, logical_pages + 1):
                src.write(bytes([pgno]) * PAGE_SZ)
            src.write(b"P" * PAGE_SZ * preallocated_pages)

    def _build_and_promote(self) -> int:
        hashes, how = self.db._build_cache(self.src, self.tmp, self.dst, b"key")
        with open(self.tmp + ".pages", "wb") as pages_file:
            pages_file.write(hashes)
        self.assertTrue(WeChatDB._promote_cache(self.tmp, self.dst))
        self.assertTrue(how.startswith("增量 "))
        return int(how.removeprefix("增量 ").removesuffix(" 页"))

    def _write_committed_wal(self, logical_pages: int) -> str:
        """生成仅含 page1 提交帧的 WAL，用于修正缓存逻辑页数。"""
        wal_path = self.src + "-wal"
        salt = b"testsalt"
        page1 = bytearray(PAGE_SZ)
        page1[:16] = b"SQLite format 3\x00"
        page1[28:32] = struct.pack(">I", logical_pages)
        header = b"".join((
            struct.pack(">IIII", 0x377F0682, 3007000, PAGE_SZ, 0),
            salt,
            b"\x00" * 8,
        ))
        frame_header = b"".join((
            struct.pack(">II", 1, logical_pages),
            salt,
            b"\x00" * 8,
        ))
        with open(wal_path, "wb") as wal:
            wal.write(header)
            wal.write(frame_header)
            wal.write(page1)
        return wal_path

    def test_unchanged_preallocated_pages_are_not_decrypted_repeatedly(self) -> None:
        """逻辑范围外的预分配页无需解密，缓存截断后仍应增量 0 页。"""
        self._write_source()
        with patch("wechatauto.db._decrypt_page", side_effect=lambda key, page, pgno: page):
            hashes = []
            self.db._decrypt_file(self.src, self.dst, b"key", hashes_out=hashes)
            with open(self.dst + ".pages", "wb") as pages_file:
                pages_file.write(b"".join(hashes))
            self.assertEqual(os.path.getsize(self.dst), 2 * PAGE_SZ)

            # 修复前每轮都固定解密三个已截断尾页，对应日志中的固定 1785 页。
            self.assertEqual(self._build_and_promote(), 0)
            self.assertEqual(self._build_and_promote(), 0)
            self.assertEqual(self._build_and_promote(), 0)

    def test_changed_logical_page_is_still_decrypted(self) -> None:
        """保留物理尾页不能掩盖逻辑数据页变化。"""
        self._write_source()
        with patch("wechatauto.db._decrypt_page", side_effect=lambda key, page, pgno: page) as decrypt:
            source_hashes = []
            self.db._decrypt_file(self.src, self.dst, b"key", hashes_out=source_hashes)
            with open(self.dst + ".pages", "wb") as pages_file:
                pages_file.write(b"".join(source_hashes))
            self._build_and_promote()
            decrypt.reset_mock()

            with open(self.src, "r+b") as src:
                src.seek(PAGE_SZ)
                src.write(b"Z" * PAGE_SZ)
            self.assertEqual(self._build_and_promote(), 1)
            # page1 每轮需解密以排除 WAL overlay；实际改写的仍只有第 2 页。
            self.assertEqual(decrypt.call_count, 2)
            with open(self.dst, "rb") as cache:
                cache.seek(PAGE_SZ)
                self.assertEqual(cache.read(PAGE_SZ), b"Z" * PAGE_SZ)

    def test_preallocated_page_is_materialized_when_it_becomes_logical(self) -> None:
        """逻辑库扩容后，此前跳过的尾页必须及时解密并写入缓存。"""
        self._write_source()
        with patch("wechatauto.db._decrypt_page", side_effect=lambda key, page, pgno: page) as decrypt:
            source_hashes = []
            self.db._decrypt_file(self.src, self.dst, b"key", hashes_out=source_hashes)
            with open(self.dst + ".pages", "wb") as pages_file:
                pages_file.write(b"".join(source_hashes))
            self.assertEqual(self._build_and_promote(), 0)
            decrypt.reset_mock()

            with open(self.src, "r+b") as src:
                src.seek(28)
                src.write(struct.pack(">I", 4))
            # page1 加上新纳入逻辑范围的第 3、4 页，共三页。
            self.assertEqual(self._build_and_promote(), 3)
            self.assertEqual(decrypt.call_count, 3)
            self.assertEqual(os.path.getsize(self.dst), 4 * PAGE_SZ)

    def test_main_rebuild_ignores_wal_overlaid_page1(self) -> None:
        """主库重建不得沿用 WAL 提交帧写入缓存的逻辑页数。"""
        self._write_source(logical_pages=4, preallocated_pages=1)
        with patch("wechatauto.db._decrypt_page", side_effect=lambda key, page, pgno: page) as decrypt:
            source_hashes = []
            self.db._decrypt_file(self.src, self.dst, b"key", hashes_out=source_hashes)
            with open(self.dst + ".pages", "wb") as pages_file:
                pages_file.write(b"".join(source_hashes))

            wal_path = self._write_committed_wal(logical_pages=2)
            self.assertEqual(self.db._merge_wal(self.dst, wal_path, b"key", 0), (1, 1))
            self.assertEqual(os.path.getsize(self.dst), 2 * PAGE_SZ)
            decrypt.reset_mock()

            # WAL 已把缓存截成两页，但主库仍声明四页。重新构建主库时，
            # page1 和缓存中缺失的第 3、4 页都必须恢复，预分配尾页仍跳过。
            self.assertEqual(self._build_and_promote(), 3)
            self.assertEqual(decrypt.call_count, 3)
            self.assertEqual(os.path.getsize(self.dst), 4 * PAGE_SZ)
            with open(self.dst, "rb") as cache:
                page1 = cache.read(PAGE_SZ)
            self.assertEqual(struct.unpack(">I", page1[28:32])[0], 4)

            # 恢复后的主库缓存再次重建应收敛，不能反复解密缺失页。
            self.assertEqual(self._build_and_promote(), 0)

    # ------------------------------------------------------------------
    # 后台重建健壮性：失败可见 / 快速重试 / 并发上限 / 陈旧告警
    # ------------------------------------------------------------------
    def _run_bg_rebuild(self, rel: str = "message/message_8.db") -> tuple:
        """同步执行一次后台重建，返回 (捕获的 stderr 文本, 耗时秒)。

        用临时伪页目录模拟源库，不触碰真实微信数据库；``_wal_path``
        置空以隔离 WAL 合并路径。
        """
        started = time.monotonic()
        buf = io.StringIO()
        with patch.object(self.db, "_wal_path", return_value=None), \
                patch("wechatauto.db._decrypt_page",
                      side_effect=lambda key, page, pgno: page), \
                contextlib.redirect_stderr(buf):
            self.db._bg_rebuild(rel, self.src, self.dst, b"key",
                                os.path.getmtime(self.src),
                                os.path.getsize(self.src), 0.0, 0)
        return buf.getvalue(), time.monotonic() - started

    def _capture_stale_warning(self, rel: str) -> str:
        """调用一次陈旧告警检查，返回捕获的 stderr 文本。"""
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.db._maybe_warn_stale(rel)
        return buf.getvalue()

    def test_bg_rebuild_failure_is_logged(self) -> None:
        """三次尝试全失败必须留日志（分片、原因、连续次数），不再静默 return。"""
        self._write_source()
        with patch.object(self.db, "_build_cache",
                          side_effect=OSError("源库被占用")):
            out, cost = self._run_bg_rebuild()

        self.assertIn("缓存重建失败", out)
        self.assertIn("message/message_8.db", out)
        self.assertIn("源库被占用", out)
        self.assertIn("连续 1 次", out)
        # 每轮首次异常留一次完整 traceback（重构引入的 AttributeError 才能定位）
        self.assertEqual(out.count("Traceback (most recent call last)"), 1)
        # 第 3 次尝试之后不再 sleep，文案不得声称还会重试
        self.assertIn("已达最大尝试次数，进入收尾（不再重试）", out)
        self.assertNotIn("1.0s 后重试", out)
        self.assertFalse(os.path.exists(self.tmp))          # 临时文件已清理
        # 快速失败快速重试：0.2s 间隔，不应是 1.5s/1.0s 的撕裂退避
        self.assertLess(cost, 2.0)
        status = {item["rel"]: item for item in self.db.cache_status()}
        item = status["message/message_8.db"]
        self.assertIn("源库被占用", item["last_error"])
        # last_error 进 /api/status：必须带栈（可定位）但限长（不撑大响应）
        self.assertIn("Traceback", item["last_error"])
        self.assertLessEqual(len(item["last_error"]), MAX_CACHE_ERROR_LEN)
        self.assertNotIn("\n", item["last_error"])
        self.assertEqual(item["fail_streak"], 1)
        self.assertIsNone(item["stale_seconds"])             # 从未成功 promote

    def test_bg_rebuild_validation_failure_is_logged(self) -> None:
        """校验失败同样必须留痕（含 quick_check 原因）。"""
        self._write_source()
        with patch.object(self.db, "_build_cache", return_value=(b"", "全量")), \
                patch.object(self.db, "_check_merged", return_value=False):
            out, _cost = self._run_bg_rebuild()

        self.assertIn("缓存重建失败", out)
        self.assertIn("quick_check 校验失败", out)
        self.assertIn("连续 1 次", out)
        self.assertFalse(os.path.exists(self.dst))           # 坏缓存不得晋升

    def test_source_change_skips_expensive_check_and_retries_fast(self) -> None:
        """源库持续写入时只在最后一轮付整库校验，其余轮短间隔重试。"""
        self._write_source()
        real_build = self.db._build_cache
        checks = []

        def changing_build(src, tmp, dst, key, stats=None):
            # 每轮构建期间模拟微信继续写库（源库 size 变化 → 快照过期）
            with open(src, "ab") as src_file:
                src_file.write(b"X" * PAGE_SZ)
            return real_build(src, tmp, dst, key, stats=stats)

        def counting_check(path):
            checks.append(path)
            return True

        with patch.object(self.db, "_build_cache", side_effect=changing_build), \
                patch.object(self.db, "_check_merged", side_effect=counting_check):
            out, cost = self._run_bg_rebuild()

        self.assertEqual(len(checks), 1)     # 只有最后一次尝试做整库校验
        self.assertIn("缓存重建完成", out)
        self.assertLess(cost, 2.0)           # 快重试而非逐轮盲等
        self.assertTrue(os.path.exists(self.dst))   # 仍推进出可用快照

    def test_bg_rebuild_skips_when_concurrency_quota_exhausted(self) -> None:
        """配额占满时本轮跳过（不阻塞、不重建），释放后可以继续推进。"""
        self._write_source()
        for _ in range(MAX_CONCURRENT_REBUILDS):
            self.db._rebuild_sem.acquire()
        self.db._rebuilding.add("message/message_8.db")
        with patch.object(self.db, "_build_cache") as build:
            out, cost = self._run_bg_rebuild()

        build.assert_not_called()
        self.assertIn("等待重建配额超时", out)
        self.assertLess(cost, 0.5)
        # 标记必须清掉，否则该分片再也不会被重新调度
        self.assertNotIn("message/message_8.db", self.db._rebuilding)
        self.assertFalse(os.path.exists(self.dst))

        # 释放一个配额后同一分片可以正常重建
        self.db._rebuild_sem.release()
        with patch.object(self.db, "_check_merged", return_value=True):
            out2, _cost2 = self._run_bg_rebuild()
        self.assertIn("缓存重建完成", out2)
        self.assertTrue(os.path.exists(self.dst))

    def test_unchanged_cache_skips_whole_db_check(self) -> None:
        """无任何页改写时跳过整库校验（缓存与上次已验证状态一致）。"""
        self._write_source()
        with patch("wechatauto.db._decrypt_page",
                   side_effect=lambda key, page, pgno: page):
            hashes = []
            self.db._decrypt_file(self.src, self.dst, b"key", hashes_out=hashes)
            with open(self.dst + ".pages", "wb") as pages_file:
                pages_file.write(b"".join(hashes))

        with patch.object(self.db, "_check_merged") as check:
            out, _cost = self._run_bg_rebuild()

        check.assert_not_called()
        self.assertIn("增量 0 页", out)
        self.assertIn("缓存重建完成", out)
        status = {item["rel"]: item for item in self.db.cache_status()}
        self.assertIsNotNone(status["message/message_8.db"]["stale_seconds"])

    def test_stale_cache_warns_once_per_interval(self) -> None:
        """缓存超过阈值未更新时告警，且同分片 60 秒内不重复刷屏。"""
        rel = "message/message_8.db"
        self.db._record_cache_ok(rel)
        self.db._cache_ok_at[rel] = time.time() - (CACHE_STALE_WARN_SEC + 10)
        self.db._record_cache_error(rel, "quick_check 校验失败（快照撕裂）")

        first = self._capture_stale_warning(rel)
        second = self._capture_stale_warning(rel)
        self.assertIn("WARNING", first)
        self.assertIn(rel, first)
        self.assertIn("quick_check 校验失败", first)
        self.assertEqual(second, "")          # 去重生效

        # 刚成功更新过的分片不告警
        fresh = "message/message_0.db"
        self.db._record_cache_ok(fresh)
        self.assertEqual(self._capture_stale_warning(fresh), "")

    def test_bg_rebuild_contains_unexpected_exception(self) -> None:
        """R3：非 OSError/sqlite3 的未知异常同样必须被捕获、留痕、计数。"""
        self._write_source()
        with patch.object(self.db, "_build_cache",
                          side_effect=RuntimeError("未知异常类型")):
            out, _cost = self._run_bg_rebuild()

        self.assertIn("RuntimeError", out)
        self.assertIn("缓存重建失败", out)
        item = {i["rel"]: i for i in self.db.cache_status()}["message/message_8.db"]
        self.assertEqual(item["fail_streak"], 1)
        self.assertIn("未知异常类型", item["last_error"])

    def test_global_quota_returned_on_every_exit_path(self) -> None:
        """所有退出路径都必须归还配额（成功/校验失败/未知异常/晋升失败）。

        配额泄漏会让后续分片永远拿不到重建机会（正是"停摆"的成因），
        因此这里对每条路径逐一断言 semaphore 计数回到上限。
        """
        self._write_source()
        sem = self.db._rebuild_sem
        cases = (
            ("成功", [patch.object(self.db, "_check_merged", return_value=True)]),
            ("校验失败", [patch.object(self.db, "_build_cache",
                                       return_value=(b"", "全量")),
                          patch.object(self.db, "_check_merged",
                                       return_value=False)]),
            ("未知异常", [patch.object(self.db, "_build_cache",
                                       side_effect=RuntimeError("boom"))]),
            ("晋升失败", [patch.object(self.db, "_check_merged", return_value=True),
                          patch.object(self.db, "_promote_cache",
                                       return_value=False)]),
        )
        for name, patchers in cases:
            with contextlib.ExitStack() as stack:
                for p in patchers:
                    stack.enter_context(p)
                self._run_bg_rebuild()
            self.assertEqual(sem._value, MAX_CONCURRENT_REBUILDS,
                             "%s 路径未归还重建配额" % name)
            self.assertNotIn("message/message_8.db", self.db._rebuilding,
                             "%s 路径残留 rebuilding 标记" % name)

    def test_outer_escape_is_recorded_and_quota_returned(self) -> None:
        """内层 try 之外的调用（如 stamp 写盘）逃出的异常也必须记账。

        这类异常过去只会由 threading 打裸 traceback：配额与标记会被 finally
        正确归还（不会停摆），但 ``cache_status()`` 的 fail_streak/last_error
        永远停在旧值，运维无从判断是"没变化"还是"一直在炸"。
        """
        self._write_source()
        sem = self.db._rebuild_sem
        rel = "message/message_8.db"
        buf = io.StringIO()
        with patch.object(self.db, "_wal_path", return_value=None), \
                patch("wechatauto.db._decrypt_page",
                      side_effect=lambda key, page, pgno: page), \
                patch.object(self.db, "_check_merged", return_value=True), \
                patch.object(self.db, "_write_stamp",
                             side_effect=OSError("磁盘已满")), \
                contextlib.redirect_stderr(buf):
            with self.assertRaises(OSError):
                self.db._bg_rebuild(rel, self.src, self.dst, b"key",
                                    os.path.getmtime(self.src),
                                    os.path.getsize(self.src), 0.0, 0)

        out = buf.getvalue()
        self.assertIn("缓存重建异常逃出", out)
        self.assertIn("磁盘已满", out)
        self.assertIn("Traceback (most recent call last)", out)
        item = {i["rel"]: i for i in self.db.cache_status()}[rel]
        self.assertEqual(item["fail_streak"], 1)
        self.assertIn("磁盘已满", item["last_error"])
        self.assertIn("Traceback", item["last_error"])
        self.assertLessEqual(len(item["last_error"]), MAX_CACHE_ERROR_LEN)
        self.assertEqual(sem._value, MAX_CONCURRENT_REBUILDS,
                         "逃出路径未归还重建配额")
        self.assertNotIn(rel, self.db._rebuilding, "逃出路径残留 rebuilding 标记")

    def _rebuilding_flag(self, rel: str) -> bool:
        """读 cache_status() 中该分片的 rebuilding 标记。"""
        item = {i["rel"]: i for i in self.db.cache_status()}.get(rel)
        return bool(item and item["rebuilding"])

    def test_sync_rebuild_marks_rebuilding_flag(self) -> None:
        """R4：同步重建期间 rebuilding 可见，结束后自动清除。"""
        rel = "message/message_8.db"
        self.assertFalse(self._rebuilding_flag(rel))
        with self.db._syncing_cache(rel):
            self.assertTrue(self._rebuilding_flag(rel), "同步重建未暴露 rebuilding")
        self.assertFalse(self._rebuilding_flag(rel), "同步重建结束后未清除标记")

    def test_sync_rebuild_keeps_background_mark(self) -> None:
        """同步标记不抢占/不清除后台重建已有的标记。"""
        rel = "message/message_1.db"
        with self.db._rebuild_guard:
            self.db._rebuilding.add(rel)
        with self.db._syncing_cache(rel):
            self.assertTrue(self._rebuilding_flag(rel))
        self.assertIn(rel, self.db._rebuilding,
                      "同步路径清掉了后台重建的标记")

    def test_cache_status_resets_after_success(self) -> None:
        """成功 promote 后清零连续失败并刷新 stale_seconds。"""
        rel = "message/message_1.db"
        self.db._record_cache_error(rel, "构建异常 OSError: 磁盘忙")
        self.db._record_cache_error(rel, "构建异常 OSError: 磁盘忙")
        failed = {item["rel"]: item for item in self.db.cache_status()}[rel]
        self.assertEqual(failed["fail_streak"], 2)
        self.assertIn("磁盘忙", failed["last_error"])

        self.db._record_cache_ok(rel)
        ok = {item["rel"]: item for item in self.db.cache_status()}[rel]
        self.assertEqual(ok["fail_streak"], 0)
        self.assertEqual(ok["last_error"], "")
        self.assertIsNotNone(ok["stale_seconds"])
        self.assertLess(ok["stale_seconds"], 5.0)
        self.assertTrue(ok["last_ok_at"])


if __name__ == "__main__":
    unittest.main()
