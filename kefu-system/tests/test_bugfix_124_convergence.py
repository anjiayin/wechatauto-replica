# -*- coding: utf-8 -*-
"""1.2.4 打包版四项故障的回归测试。

1. 图片模板每次 load_image 全盘 glob → TTL + 目录 mtime 签名缓存 + 单飞；
2. 源库持续写入时后台重建永不 promote/stamp → 收敛、落戳、不误加失败；
3. 图片 AES 密钥「已落盘仍报失败」→ 账号键读写一致 + 多样本探针；
4. 密钥就绪后不主动重试待补识别图片 → ImageRetryQueue.wake_all()。

使用仓库现有 pytest 配置与 Python 环境，全部为离线伪数据，不触碰真实微信库。
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import pytest
from PIL import Image
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# 仓库根（wechatauto 包父级）
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import wechatauto.media as media_mod
from wechatauto.db import MAX_CONCURRENT_REBUILDS, PAGE_SZ, WeChatDB
from wechatauto.media import V2_MAGIC, MediaDownloader, xor_bytes

from app.image_retry import ImageRetryQueue

CHAT = "12345@chatroom"
MD5 = "87ffb0b0765e5e9fa05d978a25155348"
AES_KEY = "d2595b4f2e57c669"       # 现场 image_keys.json 中的真实密钥
AES_KEY_OTHER = "0123456789abcdef"
XOR_KEY = 0xF9                      # 本机账号级 XOR key（批次 10 实测）
ACCOUNT = "wxid_hu7azjb674f721_ac0e"


# ---------------------------------------------------------------------------
# 公共夹具
# ---------------------------------------------------------------------------
class _FakeDb:
    """MediaDownloader 所需的最小 db 替身。"""

    def __init__(self, account_dir: str, workdir: str,
                 account: str = ACCOUNT) -> None:
        self.account_dir = account_dir
        self.workdir = workdir
        self.account = account
        self.wxid = "wxid_hu7azjb674f721"
        self.cfg_dword = None

    def get_message_row(self, user: str, local_id: int):
        return None

    def _find_weixin_pids(self):
        return []


def _downloader(tmp_path, account: str = ACCOUNT,
                image_key=None) -> MediaDownloader:
    acct = str(tmp_path / "account")
    work = str(tmp_path / "work")
    os.makedirs(acct, exist_ok=True)
    os.makedirs(work, exist_ok=True)
    return MediaDownloader(_FakeDb(acct, work, account),
                           save_dir=str(tmp_path), image_key=image_key)


def _jpeg() -> bytes:
    """有足够颜色变化的噪声 JPEG，能通过 is_usable_image。"""
    img = Image.new("RGB", (200, 150))
    px = img.load()
    for y in range(150):
        for x in range(200):
            px[x, y] = ((x * 9) % 256, (y * 5 + x) % 256, (x + y * 3) % 256)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _v2_dat(plain: bytes, aes_key: str = AES_KEY,
            xor_key: int = XOR_KEY) -> bytes:
    """按微信 V2 格式造 .dat（前 1024B AES-ECB，其余单字节 XOR）。"""
    head, rest = plain[:1024], plain[1024:]
    pad = 16 - len(head) % 16
    padded = head + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(aes_key.encode()), modes.ECB()).encryptor()
    cipher = enc.update(padded) + enc.finalize()
    return (V2_MAGIC + struct.pack("<LL", 1024, len(rest)) + b"\x00"
            + cipher + xor_bytes(rest, xor_key))


def _write_template(dl: MediaDownloader, name: str, data: bytes,
                    mtime: float, month: str = "2026-11") -> str:
    """写到 attach/<会话md5>/<YYYY-MM>/Img，并指定 mtime（控制排序）。"""
    chat_md5 = hashlib.md5(CHAT.encode()).hexdigest()
    folder = os.path.join(dl.db.account_dir, "msg", "attach", chat_md5,
                          month, "Img")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as handle:
        handle.write(data)
    os.utime(path, (mtime, mtime))
    return path


# ---------------------------------------------------------------------------
# 1. 模板扫描缓存
# ---------------------------------------------------------------------------
class TestTemplateScanCache:
    def _init_dirs(self, dl: MediaDownloader) -> str:
        chat_md5 = hashlib.md5(CHAT.encode()).hexdigest()
        img = os.path.join(dl.db.account_dir, "msg", "attach", chat_md5,
                           "2026-11", "Img")
        os.makedirs(img, exist_ok=True)
        _write_template(dl, "a_t.dat", _v2_dat(_jpeg()), time.time() - 100)
        return img

    def test_cache_hit_then_ttl_refresh_without_rescan(self, tmp_path):
        """TTL 内零扫描；TTL 到期但目录签名未变时只延长 TTL。"""
        dl = _downloader(tmp_path)
        img_dir = self._init_dirs(dl)
        calls = []
        real_scan = dl._scan_templates

        def counting(base, keep):
            calls.append(base)
            return real_scan(base, keep)

        with patch.object(dl, "_scan_templates", side_effect=counting):
            first = dl._collect_templates()
            second = dl._collect_templates()
            assert first == second
            assert len(calls) == 1, "TTL 内不应重复扫描"

            base = os.path.join(dl.db.account_dir, "msg", "attach")
            cached = list(dl._tpl_cache[base])
            cached[0] = 0.0                     # 模拟 TTL 到期
            dl._tpl_cache[base] = tuple(cached)
            third = dl._collect_templates()
            assert third == first
            assert len(calls) == 1, "目录签名未变不得重新列目录"

            # 新图片写入 + TTL 再次到期 → 目录 mtime 变化必须触发重新扫描
            _write_template(dl, "b_t.dat", _v2_dat(_jpeg()), time.time())
            os.utime(img_dir, (time.time() + 2, time.time() + 2))
            cached = list(dl._tpl_cache[base])
            cached[0] = 0.0
            dl._tpl_cache[base] = tuple(cached)
            fourth = dl._collect_templates()
            assert len(fourth) == 2
            assert len(calls) == 2

    def test_new_month_dir_in_existing_chat_triggers_rescan(self, tmp_path):
        """已有会话下新增月份目录只改会话目录 mtime，也必须触发重扫。"""
        dl = _downloader(tmp_path)
        chat_md5 = hashlib.md5(CHAT.encode()).hexdigest()
        base = os.path.join(dl.db.account_dir, "msg", "attach")
        old_img = os.path.join(base, chat_md5, "2026-10", "Img")
        os.makedirs(old_img, exist_ok=True)
        old_path = _write_template(dl, "a_t.dat", _v2_dat(_jpeg()),
                                   time.time() - 100, month="2026-10")
        assert os.path.isfile(old_path)

        calls = []
        real_scan = dl._scan_templates

        def counting(scan_base, keep):
            calls.append(scan_base)
            return real_scan(scan_base, keep)

        with patch.object(dl, "_scan_templates", side_effect=counting):
            first = dl._collect_templates()
            assert old_path in first
            assert len(calls) == 1

            # TTL 到期但目录层级未变 → 只延长 TTL，不重扫。
            cached = list(dl._tpl_cache[base])
            cached[0] = 0.0
            dl._tpl_cache[base] = tuple(cached)
            sig_before = dl._tpl_cache[base][1]
            assert dl._collect_templates() == first
            assert len(calls) == 1

            # 同一会话下新建 2026-11/Img/c_t.dat：只改会话目录的 mtime。
            new_path = _write_template(dl, "c_t.dat", _v2_dat(_jpeg()),
                                       time.time(), month="2026-11")
            assert os.path.isfile(new_path)
            sig_after = dl._dirs_signature(base, dl._tpl_cache[base][2])
            assert sig_after != sig_before, "新增月份目录必须改变目录签名"

            cached = list(dl._tpl_cache[base])
            cached[0] = 0.0
            dl._tpl_cache[base] = tuple(cached)
            found = dl._collect_templates()
            assert new_path in found, "跨月新增的模板必须被发现"
            assert len(calls) == 2, "签名变化后必须重新列目录"

    def test_concurrent_calls_share_one_scan(self, tmp_path):
        """16 个并发调用只允许一次实际扫描（vision worker 并发场景）。"""
        dl = _downloader(tmp_path)
        self._init_dirs(dl)
        calls = []
        real_scan = dl._scan_templates

        def slow_scan(base, keep):
            calls.append(1)
            time.sleep(0.15)
            return real_scan(base, keep)

        results: list = []
        errors: list = []
        barrier = threading.Barrier(16)

        def worker():
            try:
                barrier.wait()
                results.append(dl._collect_templates())
            except Exception as exc:            # pragma: no cover - 异常要显式暴露
                errors.append(exc)

        with patch.object(dl, "_scan_templates", side_effect=slow_scan):
            threads = [threading.Thread(target=worker) for _ in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        assert not errors, errors
        assert len(results) == 16
        assert len(calls) == 1, "并发调用各自全盘扫描会再次拖垮商机页面"
        assert all(item == results[0] for item in results)

    def test_account_xor_voting_still_uses_cached_templates(self, tmp_path):
        """缓存不能改变 XOR key 的账号级投票/实测正确性（本机 0xF9）。"""
        dl = _downloader(tmp_path, image_key=AES_KEY)
        for index in range(5):
            _write_template(dl, "%032x_t.dat" % (index + 1),
                            _v2_dat(_jpeg()), time.time() - index)
        assert dl._learn_account_xor_key() == XOR_KEY
        assert dl._learn_account_xor_key() == XOR_KEY   # 命中学习结果缓存


# ---------------------------------------------------------------------------
# 2. 源库持续变化时后台重建收敛
# ---------------------------------------------------------------------------
class _RebuildHarness(unittest.TestCase):
    """复刻 tests/test_db_cache.py 的轻量 WeChatDB 实例。"""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.src = os.path.join(self.tempdir.name, "message_8.db")
        self.dst = os.path.join(self.tempdir.name, "cache.db")
        self.rel = "message/message_8.db"
        self.db = WeChatDB.__new__(WeChatDB)
        self.db._rebuild_guard = threading.Lock()
        self.db._rebuild_locks = {}
        self.db._rebuilding = set()
        self.db._rebuild_done_at = {}
        self.db._rebuild_sem = threading.Semaphore(MAX_CONCURRENT_REBUILDS)
        self.db._ensure_cache_state()
        with open(self.src, "wb") as handle:
            page = bytearray(PAGE_SZ)
            page[:16] = b"SQLite format 3\x00"
            handle.write(page)

    def run_rebuild(self, **patches):
        buf = io.StringIO()
        patchers = [
            patch.object(self.db, "_wal_path", return_value=None),
            patch("wechatauto.db._decrypt_page",
                  side_effect=lambda key, page, pgno: page),
        ]
        patchers.extend(patches.values())
        for item in patchers:
            item.start()
        try:
            with contextlib.redirect_stderr(buf):
                self.db._bg_rebuild(self.rel, self.src, self.dst, b"key",
                                    os.path.getmtime(self.src),
                                    os.path.getsize(self.src), 0.0, 0)
        finally:
            for item in reversed(patchers):
                item.stop()
        return buf.getvalue()


class TestBackgroundRebuildConvergence(_RebuildHarness):
    def test_churn_rebuild_promotes_stamps_and_does_not_increment_fail(self):
        """源库在构建期间持续变化也必须 promote + 落戳，且不误加失败。"""
        src_mtime = os.path.getmtime(self.src)
        src_size = os.path.getsize(self.src)
        real_build = WeChatDB._build_cache

        def growing_build(*args, **kwargs):
            with open(self.src, "ab") as handle:
                handle.write(b"X" * PAGE_SZ)
            return real_build(self.db, *args, **kwargs)

        out = self.run_rebuild(
            build=patch.object(self.db, "_build_cache",
                               side_effect=growing_build),
            check=patch.object(self.db, "_check_merged", return_value=True),
        )

        self.assertIn("缓存重建完成", out)
        self.assertNotIn("缓存重建失败", out)
        self.assertIn("源库持续写入", out)
        self.assertTrue(os.path.exists(self.dst), "校验通过后必须晋升缓存")
        self.assertTrue(os.path.exists(self.dst + ".stamp"),
                        "旧实现源库变化时完全不写 stamp，导致 stale 无限增长")
        stamp = self.db._read_stamp(self.dst + ".stamp")
        self.assertIsNotNone(stamp)
        # stamp 记录构建开始时签名（保守），不会把构建期间的新消息假装已包含
        self.assertEqual(stamp["size"], src_size)
        self.assertEqual(stamp["mtime"], src_mtime)

        item = {i["rel"]: i for i in self.db.cache_status()}[self.rel]
        self.assertEqual(item["fail_streak"], 0)
        self.assertEqual(item["last_error"], "")
        self.assertIsNotNone(item["stale_seconds"])
        self.assertLess(item["stale_seconds"], 5.0)

    def test_unverified_snapshot_is_never_promoted(self):
        """安全底线：源库再怎么变化，quick_check 不通过就不晋升。"""
        out = self.run_rebuild(
            build=patch.object(self.db, "_build_cache",
                               return_value=(b"", "全量")),
            check=patch.object(self.db, "_check_merged", return_value=False),
        )
        self.assertIn("缓存重建失败", out)
        self.assertFalse(os.path.exists(self.dst))
        item = {i["rel"]: i for i in self.db.cache_status()}[self.rel]
        self.assertEqual(item["fail_streak"], 1)

    def test_stale_warning_distinguishes_churn_from_real_failure(self):
        """「源库一直写」与「真的失败」必须用不同文案，不能都刷 WARNING。"""
        self.db._record_cache_ok(self.rel)
        self.db._cache_ok_at[self.rel] = time.time() - 400
        self.db._record_cache_churn(self.rel)

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.db._maybe_warn_stale(self.rel)
        churn_text = buf.getvalue()
        self.assertIn("[wechatauto] INFO", churn_text)
        self.assertNotIn("WARNING", churn_text)
        self.assertNotIn("轮询可能读不到新消息", churn_text)

        self.db._record_cache_error(self.rel, "quick_check 校验失败（快照撕裂）")
        self.db._stale_warned_at.pop(self.rel, None)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.db._maybe_warn_stale(self.rel)
        real_text = buf.getvalue()
        self.assertIn("WARNING", real_text)
        self.assertIn("连续失败 1 次", real_text)

    def test_rebuilding_long_first_rebuild_is_not_false_warning(self):
        """长时首次重建进行中（churn 标记还没落）不能误报 WARNING。

        现场：删缓存后 message_8.db 首次全量重建耗时 159.6s，期间微信持续
        写库。churn 标记只在构建结束后发现签名变化时才落，所以构建中途
        stale 越过 120s、fail_streak=0、last_error 为空——旧实现打出
        「WARNING ... 连续失败 0 次，最后错误: 无」自相矛盾的告警。
        """
        self.db._record_cache_ok(self.rel)
        self.db._cache_ok_at[self.rel] = time.time() - 200
        # 没有任何 churn 记录：模拟首次重建还没走到签名比对那一步
        self.db._cache_churn_at.pop(self.rel, None)
        self.db._rebuilding.add(self.rel)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                self.db._maybe_warn_stale(self.rel)
            text = buf.getvalue()
            self.assertIn("[wechatauto] INFO", text)
            self.assertNotIn("WARNING", text)
            self.assertNotIn("轮询可能读不到新消息", text)
        finally:
            self.db._rebuilding.discard(self.rel)


# ---------------------------------------------------------------------------
# 3. 图片 AES 密钥：account 键一致 + 多探针
# ---------------------------------------------------------------------------
class TestPersistedImageKey:
    def test_account_key_is_read_with_probe_not_from_first_glob_hit(self, tmp_path):
        """image_keys.json 用 db.account 作键；正确 key 不能被损坏样本探针否掉。"""
        dl = _downloader(tmp_path)
        store = os.path.join(dl.db.workdir, "image_keys.json")
        with open(store, "w", encoding="utf-8") as handle:
            json.dump({ACCOUNT: AES_KEY, "other_account": AES_KEY_OTHER},
                      handle)

        # mtime 最新的是一个损坏样本（旧实现 glob 命中第一个 → 探针无效）
        _write_template(dl, "f" * 32 + "_t.dat", b"\x00" * 64, time.time())
        # 稍早的样本用本账号 key 加密，另一份用其它账号 key 加密
        _write_template(dl, "a" * 32 + "_t.dat",
                        _v2_dat(_jpeg(), AES_KEY), time.time() - 10)
        _write_template(dl, "b" * 32 + "_t.dat",
                        _v2_dat(_jpeg(), AES_KEY_OTHER), time.time() - 20)

        assert dl._load_persisted_key() == AES_KEY, (
            "必须用 db.account 对应的 key，且不能因首个样本损坏而误判无效")

    def test_persist_uses_same_account_key(self, tmp_path):
        """写入与读取用同一个 account（含 _ac0e 后缀）。"""
        dl = _downloader(tmp_path)
        dl._persist_key(AES_KEY)
        with open(os.path.join(dl.db.workdir, "image_keys.json"),
                  encoding="utf-8") as handle:
            saved = json.load(handle)
        assert saved[ACCOUNT] == AES_KEY

    def test_wrong_account_key_is_not_used(self, tmp_path):
        """仅有别的账号 key、且样本只认本账号 key 时不得误用。"""
        dl = _downloader(tmp_path)
        with open(os.path.join(dl.db.workdir, "image_keys.json"),
                  "w", encoding="utf-8") as handle:
            json.dump({"another_wxid_ac0e": AES_KEY_OTHER}, handle)
        _write_template(dl, "c" * 32 + "_t.dat",
                        _v2_dat(_jpeg(), AES_KEY), time.time())
        assert dl._load_persisted_key() is None


# ---------------------------------------------------------------------------
# 4. 密钥就绪后主动唤醒待补识别图片
# ---------------------------------------------------------------------------
def test_key_ready_callback_wakes_pending_image_retry(tmp_path):
    """密钥捕获成功后立刻重跑待补识别队列（不再等 10/30/90s 退避）。"""
    calls = []

    def handler(task):
        calls.append(task)
        return "done"

    queue = ImageRetryQueue(handler=handler, delays=(60.0, 120.0))
    queue.enqueue(CHAT, 101, {"reason": "no_key"})
    queue.enqueue(CHAT, 102, {"reason": "no_key"})

    notified = []

    def on_key(key):
        notified.append(queue.wake_all())

    dl = _downloader(tmp_path)
    dl._key_ready_callback = on_key
    dl._notified_keys.clear()
    dl._notify_key_ready(AES_KEY)
    dl._notify_key_ready(AES_KEY)       # 同一把 key 只通知一次

    assert notified == [2], notified
    status = queue.status()
    assert status["pending"] == 2
    assert status["next_in_sec"] == 0, "唤醒后应立即到期"
