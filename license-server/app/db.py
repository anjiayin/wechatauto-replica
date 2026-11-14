# -*- coding: utf-8 -*-
"""极简 MySQL 连接池与查询封装（PyMySQL 裸 SQL，兼容 MySQL 5.6）。

要点
----
* 连接池用 stdlib ``queue.LifoQueue``，取连接时 ``ping(reconnect=True)``，
  坏连接直接丢弃重建（MySQL 会主动断开闲置连接）；
* 所有查询走 ``DictCursor``，返回 ``dict``；时间列是**东八区 naive datetime**；
* ``transaction()`` 提供游标 + 显式提交/回滚，业务写操作必须走它；
* SQL 一律参数化（``%s``），禁止任何字符串拼接用户输入；
* 不使用 ``NOW()``/``CURDATE()``：时间一律由 Python 传入（数据库时区不可控）。
"""
from __future__ import annotations

import logging
import queue
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

import pymysql
from pymysql.cursors import DictCursor

from app.config import get_config

logger = logging.getLogger(__name__)

_POOL: Optional[queue.LifoQueue] = None
_POOL_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 连接管理
# ---------------------------------------------------------------------------
def _new_connection() -> pymysql.connections.Connection:
    """新建一条 MySQL 连接（autocommit 关闭，交给事务控制）。"""
    cfg = get_config()
    return pymysql.connect(
        host=cfg.db_host,
        port=cfg.db_port,
        user=cfg.db_user,
        password=cfg.db_password,
        database=cfg.db_name,
        charset=cfg.db_charset,
        autocommit=False,
        cursorclass=DictCursor,
        connect_timeout=cfg.db_connect_timeout,
        read_timeout=60,
        write_timeout=60,
    )


def init_pool() -> queue.LifoQueue:
    """初始化连接池（幂等）。"""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            size = max(1, get_config().db_pool_size)
            _POOL = queue.LifoQueue(maxsize=size)
        return _POOL


def _acquire() -> pymysql.connections.Connection:
    """取一条可用连接（坏连接丢弃重建）。"""
    pool = init_pool()
    try:
        conn = pool.get_nowait()
    except queue.Empty:
        return _new_connection()
    try:
        conn.ping(reconnect=True)
        return conn
    except Exception as exc:                       # 连接已失效
        logger.warning("丢弃失效的数据库连接：%s", exc)
        try:
            conn.close()
        except Exception:                          # 关闭失败无需处理
            pass
        return _new_connection()


def _release(conn: pymysql.connections.Connection) -> None:
    """归还连接（池满则关闭）。"""
    pool = _POOL
    if pool is None:
        try:
            conn.close()
        except Exception:
            pass
        return
    try:
        # 归还前确保没有未提交事务残留
        if conn.get_autocommit() is False:
            conn.rollback()
    except Exception:
        pass
    try:
        pool.put_nowait(conn)
    except queue.Full:
        try:
            conn.close()
        except Exception:
            pass


def close_pool() -> None:
    """关闭池内全部连接（进程退出/测试用）。"""
    global _POOL
    with _POOL_LOCK:
        pool = _POOL
        _POOL = None
    if pool is None:
        return
    while True:
        try:
            conn = pool.get_nowait()
        except queue.Empty:
            break
        try:
            conn.close()
        except Exception:
            pass


@contextmanager
def get_conn() -> Iterator[pymysql.connections.Connection]:
    """借一条连接，正常结束提交、异常回滚，最后归还连接池。"""
    conn = _acquire()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        _release(conn)


@contextmanager
def transaction() -> Iterator[DictCursor]:
    """事务上下文：产出游标，正常结束提交，异常回滚。

    用法::

        with db.transaction() as cur:
            cur.execute(...)
            cur.execute(...)
    """
    conn = _acquire()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            cur.close()
        except Exception:
            pass
        _release(conn)


# ---------------------------------------------------------------------------
# 查询封装
# ---------------------------------------------------------------------------
def query_one(sql: str, params: Optional[Sequence[Any]] = None
              ) -> Optional[Dict[str, Any]]:
    """执行查询并返回第一行（无数据返回 None）。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params) if params else None)
            return cur.fetchone()


def query_all(sql: str, params: Optional[Sequence[Any]] = None
              ) -> List[Dict[str, Any]]:
    """执行查询并返回全部行。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params) if params else None)
            return list(cur.fetchall() or [])


def execute(sql: str, params: Optional[Sequence[Any]] = None) -> int:
    """执行单条写语句，返回受影响行数。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params) if params else None)
            return int(cur.rowcount or 0)


def execute_many(sql: str, seq_params: Sequence[Sequence[Any]]) -> int:
    """执行批量写语句，返回受影响行数。"""
    if not seq_params:
        return 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            rows = cur.executemany(sql, [tuple(p) for p in seq_params])
            return int(rows or 0)


def ping() -> bool:
    """探活（``/api/health`` 用）：能跑通 ``SELECT 1`` 即算 DB 可用。"""
    try:
        row = query_one("SELECT 1 AS ok")
        return bool(row and int(row.get("ok") or 0) == 1)
    except Exception as exc:
        logger.warning("数据库探活失败：%s", exc)
        return False


# ---------------------------------------------------------------------------
# schema.sql 执行
# ---------------------------------------------------------------------------
def split_sql_file(path: Path) -> List[str]:
    """把 ``schema.sql`` 按 ``;`` 切成语句列表（跳过空行与整行注释）。"""
    text = path.read_text(encoding="utf-8")
    lines: List[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        lines.append(raw_line)
    statements: List[str] = []
    for chunk in "\n".join(lines).split(";"):
        statement = chunk.strip()
        if statement:
            statements.append(statement)
    return statements


def execute_script(path: Path) -> int:
    """逐条执行 SQL 文件（DDL 幂等：全部 ``CREATE TABLE IF NOT EXISTS``）。"""
    statements = split_sql_file(path)
    with get_conn() as conn:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
    return len(statements)


def list_tables() -> List[str]:
    """列出当前库全部表名（初始化校验用）。"""
    rows = query_all("SHOW TABLES")
    names: List[str] = []
    for row in rows:
        if row:
            names.append(str(list(row.values())[0]))
    return sorted(names)


def to_int(value: Any) -> int:
    """把 ``Decimal``/``None``/``str`` 统一成 ``int``（5.6 的 SUM 返回 Decimal）。"""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0
