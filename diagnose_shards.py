"""诊断：定位指定会话的消息分布在哪些分片库。

用法:
    python diagnose_shards.py 安子宜

输出每个 message_N 分片中该会话 Msg_ 表的 COUNT / MAX(create_time)，
判断消息是否跨分片分布（get_messages 只读第一个命中的分片）。
"""
import os
import sys
import time
import hashlib
import sqlite3


def _md5_hex(s):
    return hashlib.md5(s.encode() if isinstance(s, str) else s).hexdigest()


def _sz(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def _mt(p):
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(p)))
    except OSError:
        return "-"


def _ts(v):
    if not v:
        return "-"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(v)))
    except (ValueError, TypeError, OverflowError):
        return str(v)


def main():
    if len(sys.argv) < 2:
        print("用法: python diagnose_shards.py <会话昵称或微信号>")
        return
    target_user = sys.argv[1]

    from wechatauto.db import WeChatDB
    db = WeChatDB()
    print("账号目录: %s" % db.account_dir)

    user = target_user
    try:
        hits = db.search_contact(target_user)
        if hits:
            user = hits[0]["username"]
            print("输入 %r -> 匹配联系人: %s (%s)" % (
                target_user, hits[0]["nick_name"], user))
    except Exception as exc:
        print("(昵称解析失败，按原文查询: %s)" % exc)

    table = "Msg_" + _md5_hex(user)
    print("目标表: %s (user=%s)\n" % (table, user))

    rels = db._message_dbs()
    print("分片总数: %d" % len(rels))
    total = 0
    for rel in rels:
        src = db._db_path(rel)
        wal = src + "-wal"
        wal_state = "无WAL" if _sz(wal) == 0 else "WAL=%dKB" % (_sz(wal) // 1024)
        conn = None
        try:
            conn = db._open(rel)
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            if row:
                cnt, mx = conn.execute(
                    "SELECT COUNT(*), MAX(create_time) FROM %s" % table
                ).fetchone()
                total += cnt
                print("  [HIT ] %-26s 主库=%5dKB(%s) %-10s 条数=%-7d 最新=%s" % (
                    rel, _sz(src) // 1024, _mt(src), wal_state, cnt, _ts(mx)))
            else:
                print("  [ -  ] %-26s 主库=%5dKB(%s) %-10s" % (
                    rel, _sz(src) // 1024, _mt(src), wal_state))
        except Exception as exc:
            print("  [ERR ] %-26s %s" % (rel, exc))
        finally:
            if conn:
                conn.close()
    print("\n目标会话总条数(所有命中分片合计): %d" % total)
    print("若多个分片 [HIT]，get_messages 只读了排序最前的那个 —— 这就是丢消息的原因。")


if __name__ == "__main__":
    main()
