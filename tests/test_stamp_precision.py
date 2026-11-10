"""回归：缓存 stamp 的时间戳必须能精确往返。

线上故障（重启后）：每个 message 分片每 6 秒一轮出现

    message\\message_N.db 缓存重建完成（增量 0 页，x.x s，校验 0.0s）

页数为 0 说明源库内容根本没变，但 stamp 判定"源库已变化"，于是无限重建。

根因：``_write_stamp`` 用 ``%f`` 写 mtime（只保留 6 位小数），
``os.path.getmtime`` 的 float 有更多有效位，例如源库实测

    源库 : 1794190031.4439065
    stamp: 1794190031.443907     ← 读回与原值不等

``_open`` 用 ``!=`` 严格比较，于是每轮都 build=True。
"""

import os
import tempfile
import unittest

from wechatauto.db import WeChatDB, _ts_eq


class StampPrecisionTest(unittest.TestCase):
    def test_roundtrip_is_lossless(self):
        """写入再读出的 mtime / wal_mtime 必须与原值完全相等。"""
        with tempfile.TemporaryDirectory() as root:
            p = os.path.join(root, "message__message_8.db.stamp")
            mtime = 1794190031.4439065
            wal_mtime = 1794190031.4884093
            WeChatDB._write_stamp(p, mtime, 387973120, wal_mtime, 4194304, 1018)

            st = WeChatDB._read_stamp(p)
            self.assertIsNotNone(st)
            self.assertEqual(st["mtime"], mtime)
            self.assertEqual(st["wal_mtime"], wal_mtime)
            self.assertEqual(st["size"], 387973120)
            self.assertEqual(st["wal_size"], 4194304)
            self.assertTrue(_ts_eq(st["mtime"], mtime))

    def test_legacy_percent_f_stamp_still_matches(self):
        """已有缓存里的旧格式 stamp（%f 精度）不能被迫重建一次。"""
        for raw in (1794190031.4439065, 1794315630.3670943,
                    1781578947.2510562, 1773909579.5877368):
            legacy = float("%f" % raw)
            self.assertTrue(_ts_eq(legacy, raw),
                            "旧 stamp %r 应视为与源库 %r 相同" % (legacy, raw))

    def test_real_change_is_detected(self):
        """真实的时间变化（毫秒级以上）仍必须触发重建。"""
        self.assertFalse(_ts_eq(1794190031.44, 1794190032.44))
        self.assertFalse(_ts_eq(0.0, 0.001))
        self.assertFalse(_ts_eq(None, 1.0))
        self.assertFalse(_ts_eq("bad", 1.0))

    def test_threshold_is_tight(self):
        """容差必须小于一次真实写入的间隔（1e-6 秒）。"""
        base = 1794190031.4439065
        self.assertTrue(_ts_eq(base, base + 5e-7))
        self.assertFalse(_ts_eq(base, base + 1e-3))


if __name__ == "__main__":
    unittest.main()
