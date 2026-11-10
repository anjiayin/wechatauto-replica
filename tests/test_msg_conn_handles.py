"""回归：消息分片连接必须被显式关闭（Windows 缓存晋升失败根因）。

线上故障：重启后日志大量出现

    message\\message_N.db 缓存重建完成但晋升失败（正式缓存被占用？）

且每条固定耗时约 10s（= 晋升重试窗口）。实测运行中的服务进程长期持有
9 个 message 分片的缓存文件句柄，Windows 下 os.replace 无法替换正式缓存。

根因：``_msg_conn`` 为定位会话表会打开**全部** message 分片，但只返回命中
的分片；未命中分片的连接既不返回给调用方、也不在函数内关闭，只能等
GC 回收。进程长期运行时这些句柄一直挂着，正式缓存永远换不上去。

本文件只使用伪造连接，不读取真实微信数据。
"""

import threading
import unittest

from wechatauto.db import WeChatDB


class _FakeConn:
    """最小只读连接替身：只记录是否被关闭。"""

    def __init__(self, name):
        self.name = name
        self.closed = False

    def execute(self, *args, **kwargs):
        return self

    def fetchone(self):
        return (1,)

    def close(self):
        self.closed = True


class MsgConnHandleTest(unittest.TestCase):
    def _make_db(self, rels, hit_rels):
        db = WeChatDB.__new__(WeChatDB)
        db._bad_shards = {}
        db._bad_shards_mtime = {}
        db._rebuild_guard = threading.Lock()
        db._rebuild_locks = {}
        conns = {rel: _FakeConn(rel) for rel in rels}
        db._message_dbs = lambda: list(rels)
        db._is_bad_shard = lambda rel: False
        db._open = lambda rel: conns[rel]
        db._find_msg_tables = lambda user, cs: [
            (c, "Msg_t") for c in cs if c.name in hit_rels
        ]
        return db, conns

    def test_unmatched_shard_connections_are_closed(self):
        """未命中分片不能把缓存句柄一直留在进程里。"""
        db, conns = self._make_db(
            ["message_0.db", "message_1.db", "message_8.db"],
            {"message_1.db"},
        )
        found = db._msg_conn("123@chatroom")

        self.assertEqual([c.name for c, _ in found], ["message_1.db"])
        # 命中分片仍由调用方消费，不得提前关闭
        self.assertFalse(conns["message_1.db"].closed)
        # 未命中的 2 个分片必须已关闭（这正是 os.replace 失败的原因）
        self.assertTrue(conns["message_0.db"].closed)
        self.assertTrue(conns["message_8.db"].closed)

    def test_all_matched_keeps_every_connection_open(self):
        """全部命中时不误关，调用方仍能查询。"""
        rels = ["message_0.db", "message_1.db"]
        db, conns = self._make_db(rels, set(rels))
        found = db._msg_conn("123@chatroom")

        self.assertEqual(len(found), 2)
        for rel in rels:
            self.assertFalse(conns[rel].closed)

    def test_no_match_closes_all(self):
        """完全没有命中时返回 None 并关闭全部连接。"""
        rels = ["message_0.db", "message_1.db"]
        db, conns = self._make_db(rels, set())
        self.assertIsNone(db._msg_conn("123@chatroom"))
        for rel in rels:
            self.assertTrue(conns[rel].closed)

    def test_run_msg_query_closes_matched_after_build(self):
        """查询结束（含成功返回）后，命中分片连接也必须关闭。"""
        db, conns = self._make_db(["message_0.db"], {"message_0.db"})
        rows = db._run_msg_query("123@chatroom", lambda found: [
            c.execute("SELECT 1").fetchone() for c, _ in found
        ])
        self.assertEqual(rows, [(1,)])
        self.assertTrue(conns["message_0.db"].closed)


class PromoteWhileOpenedTest(unittest.TestCase):
    """晋升必须能等到只读连接释放，而不是直接放弃。"""

    def test_promote_succeeds_after_reader_releases(self):
        import os
        import sqlite3
        import tempfile
        import time

        root = tempfile.mkdtemp(prefix="promote_")
        self.addCleanup(lambda: __import__("shutil").rmtree(root, True))
        dst = os.path.join(root, "cache.db")
        tmp = dst + ".rebuild"

        # 正式缓存：一个普通的只读库
        conn = sqlite3.connect(dst)
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        conn.close()

        # 临时重建结果
        conn = sqlite3.connect(tmp)
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()

        # 模拟轮询线程：持有正式缓存的只读句柄 1 秒后释放
        reader = sqlite3.connect(
            f"file:{dst}?mode=ro", uri=True, check_same_thread=False)
        reader.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()

        def release_later():
            time.sleep(1.0)
            reader.close()

        t = threading.Thread(target=release_later, daemon=True)
        t.start()
        try:
            # 旧实现 20×0.5s 也能过；这里验证的是"占用期间不放弃、释放后成功"
            self.assertTrue(WeChatDB._promote_cache(tmp, dst))
        finally:
            t.join(timeout=10)
            try:
                reader.close()
            except Exception:
                pass
            try:
                os.remove(dst)
            except OSError:
                pass
        self.assertFalse(os.path.exists(tmp))


if __name__ == "__main__":
    unittest.main()
