# -*- coding: utf-8 -*-
"""SQLite 数据层：连接管理（WAL + RLock 写锁）、DDL 初始化、全部 DAO 函数。

线程模型：单连接 + ``threading.RLock`` 串行化所有操作（本机单写者场景），
管线/监听/API 多线程共用同一实例。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime
from typing import Any, Iterable, Optional

from app.config import (
    DB_PATH,
    DEFAULT_SETTINGS,
    MERGE_WINDOW_SEC_DEFAULT,
    MAX_MONITOR_TARGETS,
    MONITOR_MAX_TARGETS_MIN,
    MONITOR_MAX_TARGETS_MAX,
    KEY_AI_BASE_URL,
    KEY_AI_MODEL,
    KEY_ANALYSIS_MERGE_WINDOW,
    KEY_ANALYSIS_OPP_MERGE_WINDOW,
    KEY_ANALYSIS_MIN_CONFIDENCE,
    KEY_ANALYSIS_PRESCREEN_ENABLED,
    KEY_ANALYSIS_PRESCREEN_KEYWORDS,
    KEY_ANALYSIS_PRESCREEN_REGEX,
    KEY_ANALYSIS_STRICTNESS,
    KEY_AI_TIMEOUT_SEC,
    KEY_AI_TEMPERATURE,
    KEY_COMPLIANCE_CONFIRMED,
    KEY_MONITOR_MAX_TARGETS,
    KEY_NOTIFY_SOUND,
    KEY_RUNTIME_MW_MIGRATED,
)

logger = logging.getLogger(__name__)


def now_iso() -> str:
    """当前本地时间 ISO8601 字符串（共享约定：全部时间字段用它）。"""
    return datetime.now().isoformat(timespec="seconds")


def epoch_to_iso(epoch: Optional[int | float]) -> str:
    """微信消息的 create_time（epoch 秒）→ ISO8601 本地时间字符串。"""
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(int(epoch)).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return ""


# ---------------------------------------------------------------------------
# DDL（严格遵循设计文档 3.1 节，不得改动）
# ---------------------------------------------------------------------------
DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS settings (
      key        TEXT PRIMARY KEY,
      value      TEXT NOT NULL,
      updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS monitor_targets (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      username     TEXT NOT NULL UNIQUE,
      display_name TEXT NOT NULL,
      kind         TEXT NOT NULL CHECK (kind IN ('group','friend')),
      enabled      INTEGER NOT NULL DEFAULT 1,
      created_at   TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS opportunity_types (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      name        TEXT NOT NULL,
      description TEXT NOT NULL,
      enabled     INTEGER NOT NULL DEFAULT 1,
      created_at  TEXT NOT NULL DEFAULT (datetime('now')),
      updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS opportunities (
      id               INTEGER PRIMARY KEY AUTOINCREMENT,
      session_username TEXT NOT NULL,
      session_name     TEXT NOT NULL,
      sender_wxid      TEXT NOT NULL DEFAULT '',
      sender_name      TEXT NOT NULL,
      window_start     TEXT NOT NULL,
      window_end       TEXT NOT NULL,
      merged_count     INTEGER NOT NULL DEFAULT 1,
      raw_text         TEXT NOT NULL,
      type_id          INTEGER REFERENCES opportunity_types(id) ON DELETE SET NULL,
      type_name        TEXT NOT NULL,
      summary          TEXT NOT NULL,
      key_quote        TEXT NOT NULL DEFAULT '',
      confidence       INTEGER NOT NULL DEFAULT 0,
      status           TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','replied','ignored')),
      reply_content    TEXT,
      replied_at       TEXT,
      created_at       TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_opp_created ON opportunities (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_opp_status  ON opportunities (status)",
    "CREATE INDEX IF NOT EXISTS idx_opp_session ON opportunities (session_username)",
    """
    CREATE TABLE IF NOT EXISTS analysis_logs (
      id               INTEGER PRIMARY KEY AUTOINCREMENT,
      session_username TEXT NOT NULL,
      sender_wxid      TEXT,
      msg_count        INTEGER NOT NULL,
      input_text       TEXT,
      result           TEXT,
      status           TEXT NOT NULL
                       CHECK (status IN ('ok','error','skipped','below_threshold')),
      error            TEXT,
      model            TEXT,
      latency_ms       INTEGER,
      opportunity_id   INTEGER REFERENCES opportunities(id),
      created_at       TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS watermarks (
      username   TEXT PRIMARY KEY,
      sort_seq   INTEGER NOT NULL,
      updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
)


class Database:
    """kefu-system 业务 SQLite 封装（WAL + RLock）。

    Args:
        path: 数据库文件路径；``":memory:"`` 用于测试。
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = str(path or DB_PATH)
        if self.path != ":memory:":
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, timeout=15.0
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            for ddl in DDL_STATEMENTS:
                self._conn.execute(ddl)
            self._conn.commit()
        self._init_default_settings()
        self._migrate_merge_window_default()
        logger.info("业务数据库已初始化：%s", self.path)

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    def close(self) -> None:
        """关闭连接（应用退出时调用）。"""
        with self._lock:
            try:
                self._conn.commit()
                self._conn.close()
            except Exception:  # pragma: no cover - 退出路径
                pass

    def _init_default_settings(self) -> None:
        """写入缺失的默认配置键（不覆盖已有值）。"""
        with self._lock:
            for key, value in DEFAULT_SETTINGS.items():
                self._conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (key, value),
                )
            self._conn.commit()

    def _migrate_merge_window_default(self) -> None:
        """一次性迁移（优化 7）：旧版本默认微窗口 60s → 15s。

        仅当用户从未显式修改过该键（仍是出厂值 60）时才改写；
        迁移后打标记，避免用户日后主动改回 60 又被复位。
        """
        if self.get_setting(KEY_RUNTIME_MW_MIGRATED, "0") == "1":
            return
        if self.get_setting(KEY_ANALYSIS_MERGE_WINDOW, "") == "60":
            self.set_setting(KEY_ANALYSIS_MERGE_WINDOW,
                             str(MERGE_WINDOW_SEC_DEFAULT))
            logger.info("分析微窗口已从旧默认 60s 迁移为 %ds",
                        MERGE_WINDOW_SEC_DEFAULT)
        self.set_setting(KEY_RUNTIME_MW_MIGRATED, "1")

    # ------------------------------------------------------------------
    # settings DAO
    # ------------------------------------------------------------------
    def get_setting(self, key: str, default: str = "") -> str:
        """读取单个配置值。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        """写入/更新单个配置值。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (key, value, now_iso()),
            )
            self._conn.commit()

    def get_bool(self, key: str) -> bool:
        """以布尔语义读取配置（'1' 为真）。"""
        return self.get_setting(key, "0") == "1"

    def get_int(self, key: str, default: int = 0) -> int:
        """以整数语义读取配置，非法值回退默认。"""
        try:
            return int(self.get_setting(key, str(default)))
        except ValueError:
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        """以浮点语义读取配置，非法值回退默认。"""
        try:
            return float(self.get_setting(key, str(default)))
        except ValueError:
            return default

    # ------------------------------------------------------------------
    # monitor_targets DAO
    # ------------------------------------------------------------------
    def list_targets(self) -> list[dict]:
        """全部监控对象（按创建时间正序）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, username, display_name, kind, enabled, created_at "
                "FROM monitor_targets ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_target_by_username(self, username: str) -> Optional[dict]:
        """按会话 username 查监控对象。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, username, display_name, kind, enabled, created_at "
                "FROM monitor_targets WHERE username=?",
                (username,),
            ).fetchone()
        return dict(row) if row else None

    def get_target(self, target_id: int) -> Optional[dict]:
        """按 id 查监控对象。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, username, display_name, kind, enabled, created_at "
                "FROM monitor_targets WHERE id=?",
                (target_id,),
            ).fetchone()
        return dict(row) if row else None

    def insert_target(self, username: str, display_name: str, kind: str,
                      enabled: int = 1) -> dict:
        """新增监控对象（username 重复抛 sqlite3.IntegrityError）。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO monitor_targets (username, display_name, kind, enabled) "
                "VALUES (?, ?, ?, ?)",
                (username, display_name, kind, enabled),
            )
            self._conn.commit()
            return self.get_target(cur.lastrowid)  # type: ignore[return-value]

    def insert_target_within_limit(self, username: str, display_name: str,
                                   kind: str, max_targets: int,
                                   enabled: int = 1) -> Optional[dict]:
        """原子「配额校验 + 插入」（修复 BUG-3 并发越界）。

        计数与 INSERT 在同一把写锁内完成，并发添加最后 1 个名额时
        恰好达到上限、不会越过。

        Returns:
            新建对象；达到/超过上限时返回 None（调用方转 400）。
            username 重复抛 sqlite3.IntegrityError。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM monitor_targets").fetchone()
            if int(row[0]) >= max_targets:
                return None
            cur = self._conn.execute(
                "INSERT INTO monitor_targets (username, display_name, kind, "
                "enabled) VALUES (?, ?, ?, ?)",
                (username, display_name, kind, enabled),
            )
            self._conn.commit()
            return self.get_target(cur.lastrowid)  # type: ignore[return-value]

    def update_target_enabled(self, target_id: int, enabled: bool) -> None:
        """启用/停用监控对象。"""
        with self._lock:
            self._conn.execute(
                "UPDATE monitor_targets SET enabled=? WHERE id=?",
                (1 if enabled else 0, target_id),
            )
            self._conn.commit()

    def update_target_display_name(self, target_id: int,
                                   display_name: str) -> None:
        """刷新监控对象显示名快照（列表读取时补齐陈旧快照用）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE monitor_targets SET display_name=? WHERE id=?",
                (display_name, target_id),
            )
            self._conn.commit()

    def delete_target(self, target_id: int) -> int:
        """删除监控对象，返回受影响行数。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM monitor_targets WHERE id=?", (target_id,)
            )
            self._conn.commit()
            return cur.rowcount

    def count_targets(self) -> int:
        """监控对象总数（配额展示用）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM monitor_targets").fetchone()
        return int(row[0])

    def list_enabled_target_usernames(self) -> list[str]:
        """全部启用中的监控会话 username（Listener 启动注册用）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT username FROM monitor_targets WHERE enabled=1"
            ).fetchall()
        return [r["username"] for r in rows]

    # ------------------------------------------------------------------
    # opportunity_types DAO
    # ------------------------------------------------------------------
    def list_types(self, only_enabled: bool = False) -> list[dict]:
        """商机类型列表。"""
        sql = ("SELECT id, name, description, enabled, created_at, updated_at "
               "FROM opportunity_types")
        if only_enabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def get_type(self, type_id: int) -> Optional[dict]:
        """按 id 查商机类型。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, description, enabled, created_at, updated_at "
                "FROM opportunity_types WHERE id=?",
                (type_id,),
            ).fetchone()
        return dict(row) if row else None

    def insert_type(self, name: str, description: str, enabled: int = 1) -> dict:
        """新增商机类型。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO opportunity_types (name, description, enabled) "
                "VALUES (?, ?, ?)",
                (name, description, enabled),
            )
            self._conn.commit()
            return self.get_type(cur.lastrowid)  # type: ignore[return-value]

    def update_type(self, type_id: int, fields: dict[str, Any]) -> Optional[dict]:
        """更新商机类型（仅更新传入字段）。"""
        allowed = ("name", "description", "enabled")
        sets: list[str] = []
        values: list[Any] = []
        for f in allowed:
            if f in fields and fields[f] is not None:
                sets.append(f"{f}=?")
                values.append(fields[f])
        if not sets:
            return self.get_type(type_id)
        sets.append("updated_at=?")
        values.append(now_iso())
        values.append(type_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE opportunity_types SET {', '.join(sets)} WHERE id=?",
                values,
            )
            self._conn.commit()
        return self.get_type(type_id)

    def delete_type(self, type_id: int) -> int:
        """删除商机类型（关联商机 type_id 置 NULL）。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM opportunity_types WHERE id=?", (type_id,)
            )
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    # opportunities DAO
    # ------------------------------------------------------------------
    def insert_opportunity(self, *, session_username: str, session_name: str,
                           sender_wxid: str, sender_name: str,
                           window_start: str, window_end: str,
                           merged_count: int, raw_text: str,
                           type_id: Optional[int], type_name: str,
                           summary: str, key_quote: str,
                           confidence: int) -> dict:
        """插入一条商机并返回完整记录。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO opportunities (session_username, session_name, "
                "sender_wxid, sender_name, window_start, window_end, merged_count, "
                "raw_text, type_id, type_name, summary, key_quote, confidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_username, session_name, sender_wxid, sender_name,
                 window_start, window_end, merged_count, raw_text, type_id,
                 type_name, summary, key_quote, confidence),
            )
            self._conn.commit()
            return self.get_opportunity(cur.lastrowid)  # type: ignore[return-value]

    @staticmethod
    def _opp_row_to_dict(row: sqlite3.Row) -> dict:
        return dict(row)

    def list_opportunities(self, *, page: int = 1, page_size: int = 20,
                           session: str = "", type_id: Optional[int] = None,
                           status: str = "", keyword: str = "") -> tuple[int, list[dict]]:
        """商机列表（时间倒序）。

        Args:
            status: pending/replied/ignored；空串 = 排除 ignored。

        Returns:
            (total, items)
        """
        where: list[str] = []
        params: list[Any] = []
        if status in ("pending", "replied", "ignored"):
            where.append("status=?")
            params.append(status)
        else:
            where.append("status<>'ignored'")
        if session:
            where.append("session_username=?")
            params.append(session)
        if type_id:
            where.append("type_id=?")
            params.append(type_id)
        if keyword:
            where.append(
                "(summary LIKE ? OR raw_text LIKE ? OR type_name LIKE ? "
                "OR sender_name LIKE ? OR session_name LIKE ?)"
            )
            like = f"%{keyword}%"
            params.extend([like] * 5)
        where_sql = " AND ".join(where)
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM opportunities WHERE {where_sql}", params
            ).fetchone()[0]
            rows = self._conn.execute(
                "SELECT id, session_username, session_name, sender_wxid, "
                "sender_name, window_end, type_id, type_name, summary, "
                "key_quote, confidence, status, created_at FROM opportunities "
                f"WHERE {where_sql} ORDER BY created_at DESC, id DESC "
                "LIMIT ? OFFSET ?",
                [*params, page_size, (max(page, 1) - 1) * page_size],
            ).fetchall()
        return total, [dict(r) for r in rows]

    def get_opportunity(self, opp_id: int) -> Optional[dict]:
        """按 id 查商机完整记录。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM opportunities WHERE id=?", (opp_id,)
            ).fetchone()
        return self._opp_row_to_dict(row) if row else None

    def find_mergeable_opportunity(self, *, session_username: str,
                                   sender_wxid: str, sender_name: str,
                                   min_window_end_iso: str) -> Optional[dict]:
        """查找可合并商机（商机合并窗口内，pipeline 用）。

        条件：同会话 + 同发送人（wxid 为空时退化为昵称匹配）+ status='pending'
        + window_end 距今不超过商机合并窗口（ISO 字符串同格式可直接比较）。

        Args:
            min_window_end_iso: 窗口下限（now - opp_merge_window_sec 的 ISO 串）。
        """
        sender_cond = "sender_wxid=?" if sender_wxid else "sender_name=?"
        sender_val = sender_wxid or sender_name
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM opportunities WHERE session_username=? AND "
                f"{sender_cond} AND status='pending' AND window_end>=? "
                "ORDER BY window_end DESC, id DESC LIMIT 1",
                (session_username, sender_val, min_window_end_iso),
            ).fetchone()
        return self._opp_row_to_dict(row) if row else None

    def merge_opportunity(self, opp_id: int, *, raw_text_append: str,
                          summary_append: str, window_end: str,
                          merged_count_delta: int, key_quote: str,
                          confidence: int, type_id: Optional[int],
                          type_name: str) -> Optional[dict]:
        """原子合并新批次进既有商机，返回合并后的完整记录。

        修复 BUG-1/BUG-2（丢失更新竞态）：合并动作在单条 SQL 内完成——
        raw_text/summary 由 SQL 端拼接（不基于 Python 快照回写）、
        merged_count 在 SQL 端累加、confidence 取 SQL 端较大值，并以
        ``WHERE id=? AND status='pending'`` 复检状态。find 返回快照后
        商机被回复/删除/并发合并时 UPDATE 不生效，rowcount=0 返回 None，
        由调用方走新建兜底（新命中内容不被吞）。
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE opportunities SET "
                "raw_text = CASE WHEN :raw_append = '' THEN raw_text "
                "  WHEN raw_text IS NULL OR raw_text = '' THEN :raw_append "
                "  ELSE raw_text || char(10) || :raw_append END, "
                "summary = CASE WHEN :summary_append = '' THEN summary "
                "  WHEN summary IS NULL OR summary = '' THEN :summary_append "
                "  ELSE summary || char(10) || :summary_append END, "
                "window_end = :window_end, "
                "merged_count = merged_count + :count_delta, "
                "key_quote = :key_quote, "
                "confidence = MAX(confidence, :confidence), "
                "type_id = :type_id, type_name = :type_name "
                "WHERE id = :opp_id AND status = 'pending'",
                {"raw_append": raw_text_append,
                 "summary_append": summary_append,
                 "window_end": window_end,
                 "count_delta": merged_count_delta,
                 "key_quote": key_quote,
                 "confidence": confidence,
                 "type_id": type_id,
                 "type_name": type_name,
                 "opp_id": opp_id},
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            return self.get_opportunity(opp_id)

    def update_opportunity_status(self, opp_id: int, status: str) -> int:
        """更新商机状态，返回受影响行数。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE opportunities SET status=? WHERE id=?", (status, opp_id)
            )
            self._conn.commit()
            return cur.rowcount

    def set_opportunity_replied(self, opp_id: int, reply_content: str) -> int:
        """标记商机已回复（回复成功后由 Sender 调用）。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE opportunities SET status='replied', reply_content=?, "
                "replied_at=? WHERE id=?",
                (reply_content, now_iso(), opp_id),
            )
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    # analysis_logs DAO
    # ------------------------------------------------------------------
    def insert_analysis_log(self, *, session_username: str,
                            sender_wxid: str = "", msg_count: int = 0,
                            input_text: str = "", result: str = "",
                            status: str, error: str = "", model: str = "",
                            latency_ms: Optional[int] = None,
                            opportunity_id: Optional[int] = None) -> int:
        """写入一条分析日志（ok/error/skipped/below_threshold）。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO analysis_logs (session_username, sender_wxid, "
                "msg_count, input_text, result, status, error, model, "
                "latency_ms, opportunity_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_username, sender_wxid, msg_count, input_text, result,
                 status, error, model, latency_ms, opportunity_id),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    # ------------------------------------------------------------------
    # watermarks DAO
    # ------------------------------------------------------------------
    def load_watermarks(self) -> dict[str, int]:
        """读取持久化 watermark（Listener 启动时传入，避免重启重复推送）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT username, sort_seq FROM watermarks"
            ).fetchall()
        return {r["username"]: int(r["sort_seq"]) for r in rows}

    def save_watermarks(self, marks: dict[str, int]) -> None:
        """整体刷回 watermark（定期与退出时调用）。"""
        if not marks:
            return
        ts = now_iso()
        with self._lock:
            self._conn.executemany(
                "INSERT INTO watermarks (username, sort_seq, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(username) DO UPDATE SET "
                "sort_seq=excluded.sort_seq, updated_at=excluded.updated_at",
                [(u, int(s), ts) for u, s in marks.items()],
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # 聚合查询（/api/status 用）
    # ------------------------------------------------------------------
    def count_pending_opportunities(self) -> int:
        """待处理商机数。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM opportunities WHERE status='pending'"
            ).fetchone()
        return int(row[0])


# ---------------------------------------------------------------------------
# 便捷设置读取（管线与 API 共用）
# ---------------------------------------------------------------------------
def read_ai_settings(db: Database) -> dict[str, str]:
    """读取 AI 接口配置（api_key_enc 保持密文，由调用方解密）。"""
    return {
        "base_url": db.get_setting(KEY_AI_BASE_URL),
        "api_key_enc": db.get_setting("ai.api_key_enc"),
        "model": db.get_setting(KEY_AI_MODEL),
        "timeout_sec": db.get_setting(KEY_AI_TIMEOUT_SEC, "60"),
        "temperature": db.get_setting(KEY_AI_TEMPERATURE, "0.2"),
    }


def read_analysis_settings(db: Database) -> dict[str, Any]:
    """读取分析判定配置（分析微窗口 / 商机合并窗口允许 settings 覆盖）。"""
    return {
        "strictness": db.get_setting(KEY_ANALYSIS_STRICTNESS, "balanced"),
        "min_confidence": db.get_int(KEY_ANALYSIS_MIN_CONFIDENCE, 60),
        "prescreen_enabled": db.get_bool(KEY_ANALYSIS_PRESCREEN_ENABLED),
        "prescreen_keywords": db.get_setting(KEY_ANALYSIS_PRESCREEN_KEYWORDS, ""),
        "prescreen_regex": db.get_setting(KEY_ANALYSIS_PRESCREEN_REGEX, ""),
        "merge_window_sec": db.get_int(KEY_ANALYSIS_MERGE_WINDOW,
                                       MERGE_WINDOW_SEC_DEFAULT),
        "opp_merge_window_sec": db.get_int(KEY_ANALYSIS_OPP_MERGE_WINDOW, 180),
        "compliance_confirmed": db.get_bool(KEY_COMPLIANCE_CONFIRMED),
    }


def read_monitor_max_targets(db: Database) -> int:
    """读取监控对象数量上限（monitor.max_targets，越界值钳制到 10~2000）。"""
    raw = db.get_setting(KEY_MONITOR_MAX_TARGETS, str(MAX_MONITOR_TARGETS))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return MAX_MONITOR_TARGETS
    return max(MONITOR_MAX_TARGETS_MIN, min(MONITOR_MAX_TARGETS_MAX, value))


def read_notify_settings(db: Database) -> dict[str, bool]:
    """读取通知配置。"""
    return {"sound_enabled": db.get_bool(KEY_NOTIFY_SOUND)}


def iter_enabled_types(db: Database) -> Iterable[dict]:
    """启用的商机类型（每次分析实时读取注入 Prompt）。"""
    return db.list_types(only_enabled=True)
