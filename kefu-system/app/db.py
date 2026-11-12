# -*- coding: utf-8 -*-
"""SQLite 数据层：连接管理（WAL + RLock 写锁）、DDL 初始化、全部 DAO 函数。

线程模型：单连接 + ``threading.RLock`` 串行化所有操作（本机单写者场景），
管线/监听/API 多线程共用同一实例。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta
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
    KEY_AI_ENABLED,
    KEY_ANALYSIS_MERGE_WINDOW,
    KEY_ANALYSIS_OPP_MERGE_WINDOW,
    KEY_ANALYSIS_CORRECTION_EXAMPLES,
    KEY_ANALYSIS_FILTER_SELF,
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
    KEY_RUNTIME_LOCALTIME_MIGRATED,
    KEY_RUNTIME_AI_USAGE_BACKFILLED,
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
# opportunities 列定义抽出为常量：DDL 与旧库迁移共用同一份定义（防漂移）。
# 状态含 handled（用户已自行处理）——status 是 TEXT，但 CHECK 约束需要
# 对旧库做一次表重建迁移（见 _migrate_opportunity_status_handled）。
_OPPORTUNITIES_COLUMNS = """
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
      direction        TEXT NOT NULL DEFAULT '',   -- 买/卖货方向（反馈 4）：buy/sell/''=不明
      status           TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','replied','ignored','handled')),
      reply_content    TEXT,
      replied_at       TEXT,
      created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    """

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
    "CREATE TABLE IF NOT EXISTS opportunities (%s)" % _OPPORTUNITIES_COLUMNS,
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
      created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
      prompt_tokens    INTEGER,
      completion_tokens INTEGER,
      agg_wait_ms      INTEGER,
      queue_wait_ms    INTEGER,
      e2e_ms           INTEGER,
      voice_local_id   INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_logs_created ON analysis_logs (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_logs_session ON analysis_logs (session_username, created_at DESC)",
    # AI 用量独立存档（账单数据源）：与 analysis_logs 解耦，历史清理/日志
    # 重建都不会影响账单，避免「近 7 日只剩今日有数」这类数据空洞。
    """
    CREATE TABLE IF NOT EXISTS ai_usage (
      id                INTEGER PRIMARY KEY AUTOINCREMENT,
      created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
      model             TEXT NOT NULL DEFAULT '',
      session_username  TEXT NOT NULL DEFAULT '',
      prompt_tokens     INTEGER NOT NULL DEFAULT 0,
      completion_tokens INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_usage_created ON ai_usage (created_at)",
    """
    CREATE TABLE IF NOT EXISTS watermarks (
      username   TEXT PRIMARY KEY,
      sort_seq   INTEGER NOT NULL,
      updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # 群成员「微信端实际显示名」学习缓存（批次 5 需求 ②）：微信 4.x 本地库
    # 不存群名片（contact.nick_name 是全局昵称），而 session.db 的
    # last_sender_display_name 是微信客户端渲染的发送者名称（群内即群昵称/
    # 备注）。消息到达时按 (群, 成员) 落入本表，供发送人显示名解析优先采用，
    # 与用户在微信里看见的名字保持一致。持久化：重启后已学习到的名片不丢。
    """
    CREATE TABLE IF NOT EXISTS group_cards (
      chatroom   TEXT NOT NULL,
      member     TEXT NOT NULL,
      card       TEXT NOT NULL,
      updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
      PRIMARY KEY (chatroom, member)
    )
    """,
    # 语音消息转写结果缓存：同一条语音会在商机上下文/消息列表里被反复读到，
    # 每次重跑离线识别（0.5~2 秒）会明显拖慢接口，故落库缓存。
    # 注意：本表**不在** _PURGE_TABLES 白名单里 —— 转写结果与商机/日志的
    # 清理口径解耦，避免「清了日志后语音又要重新识别」。
    """
    CREATE TABLE IF NOT EXISTS voice_texts (
      username    TEXT NOT NULL,
      local_id    INTEGER NOT NULL,
      text        TEXT NOT NULL DEFAULT '',
      status      TEXT NOT NULL DEFAULT '',
      error       TEXT NOT NULL DEFAULT '',
      duration_ms INTEGER NOT NULL DEFAULT 0,
      created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
      PRIMARY KEY (username, local_id)
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
        self._migrate_opportunity_status_handled()
        self._migrate_add_columns()
        self._migrate_created_at_localtime()
        self._migrate_ai_usage_backfill()
        logger.info("业务数据库已初始化：%s", self.path)

    def _migrate_created_at_localtime(self) -> None:
        """一次性把历史 created_at 从 UTC 平移到本地时间。

        背景：旧版本 DDL 默认值为 ``datetime('now')``（UTC），前端直接展示
        导致列表时间比实际早 8 小时（用户反馈 5）。新写入改为本地时间；
        老数据按当前时区偏移整体平移一次，用设置键打标记防重复执行。
        """
        if self.get_setting(KEY_RUNTIME_LOCALTIME_MIGRATED, "0") == "1":
            return
        with self._lock:
            try:
                offset_hours = int(round(self._conn.execute(
                    "SELECT (julianday('now','localtime') - julianday('now')) * 24"
                ).fetchone()[0] or 0))
            except Exception:  # pragma: no cover - 极端环境
                logger.exception("计算本地时区偏移失败，跳过时间平移")
                return
            if offset_hours:
                modifier = f"{offset_hours:+d} hours"
                for table in ("opportunities", "analysis_logs"):
                    try:
                        self._conn.execute(
                            f"UPDATE {table} SET created_at = "
                            "datetime(created_at, ?)", (modifier,))
                    except Exception:  # pragma: no cover - 表缺失等
                        logger.exception("平移 %s.created_at 失败（已跳过）", table)
            self._conn.commit()
        self.set_setting(KEY_RUNTIME_LOCALTIME_MIGRATED, "1")
        logger.info("历史时间已从 UTC 平移为本地时间（%+d 小时）", offset_hours)

    def _migrate_ai_usage_backfill(self) -> None:
        """一次性把 analysis_logs 里已有的 token 用量回填到 ai_usage。

        背景：账单原先直接聚合 analysis_logs，日志被「历史清理」删掉后
        账单历史就出现 0 值空洞（用户反馈：近 7 日只有今日有数）。改为
        独立表存档后，这里把存量用量搬一次，保证升级后历史账单不跳变。
        失败/未调用 AI 的行（tokens 为 NULL）不回填。
        """
        if self.get_setting(KEY_RUNTIME_AI_USAGE_BACKFILLED, "0") == "1":
            return
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO ai_usage (created_at, model, session_username, "
                    "prompt_tokens, completion_tokens) "
                    "SELECT created_at, COALESCE(model, ''), "
                    "COALESCE(session_username, ''), "
                    "COALESCE(prompt_tokens, 0), COALESCE(completion_tokens, 0) "
                    "FROM analysis_logs "
                    "WHERE prompt_tokens IS NOT NULL "
                    "OR completion_tokens IS NOT NULL")
                self._conn.commit()
            except Exception:  # pragma: no cover - 表缺失等极端情况
                logger.exception("回填 AI 用量失败（已跳过，账单从新增用量开始累计）")
                return
        self.set_setting(KEY_RUNTIME_AI_USAGE_BACKFILLED, "1")
        logger.info("已回填历史 AI 用量 %d 条（账单改用独立存档）", cur.rowcount)

    def _migrate_add_columns(self) -> None:
        """幂等加列迁移（反馈 4/13）：老库补 direction 与 token 用量列。

        新建库由 DDL 直接带全列；老库按 PRAGMA table_info 检查后
        ALTER TABLE ADD COLUMN，已存在则跳过（可重复执行）。
        """
        plans = (
            ("opportunities", "direction", "TEXT NOT NULL DEFAULT ''"),
            ("analysis_logs", "prompt_tokens", "INTEGER"),
            ("analysis_logs", "completion_tokens", "INTEGER"),
            # 时序诊断（收消息→合并→排队→AI）：定位「延迟到底花在哪一段」
            ("analysis_logs", "agg_wait_ms", "INTEGER"),
            ("analysis_logs", "queue_wait_ms", "INTEGER"),
            ("analysis_logs", "e2e_ms", "INTEGER"),
            # 语音消息标识（微信会话内 local_id）：语音转写失败时记录，
            # 供补转写成功后回填日志、以及前端「重新识别」按 (会话, local_id)
            # 精确定位那条语音（消息正文里没有可用标识）
            ("analysis_logs", "voice_local_id", "INTEGER"),
        )
        with self._lock:
            for table, column, col_type in plans:
                cols = {r["name"] for r in self._conn.execute(
                    f"PRAGMA table_info({table})").fetchall()}
                if not cols or column in cols:
                    continue
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                logger.info("已迁移：%s 增加 %s 列", table, column)
            self._conn.commit()

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

    def _migrate_opportunity_status_handled(self) -> None:
        """一次性迁移：旧库 opportunities 的 CHECK 约束不含 handled 时重建表。

        status 是 TEXT，但旧 DDL 的 CHECK (status IN ('pending','replied',
        'ignored')) 会拒绝新状态值。重建步骤（foreign_keys 临时关闭）：
        建 opportunities_new（新 CHECK）→ 复制数据 → 删旧表 → 改名。
        已含 handled 的库直接跳过（幂等）。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='opportunities'").fetchone()
            if not row or "handled" in str(row["sql"] or ""):
                return
            logger.info("检测到旧版 opportunities 表（status 无 handled），"
                        "开始重建以支持「已自行处理」状态")
            self._conn.commit()
            self._conn.execute("PRAGMA foreign_keys=OFF")
            try:
                # 清理上次迁移中断的残留（DDL 自提交，残留的
                # opportunities_new 必然是建表后、复制完成前的空表，
                # 无已提交数据，可直接删除——否则重启将抛
                # "table already exists" 导致应用无法自愈，BUG-2）
                self._conn.execute(
                    "DROP TABLE IF EXISTS opportunities_new")
                self._conn.execute(
                    "CREATE TABLE opportunities_new (%s)" % _OPPORTUNITIES_COLUMNS)
                self._conn.execute(
                    "INSERT INTO opportunities_new "
                    "SELECT * FROM opportunities")
                self._conn.execute("DROP TABLE opportunities")
                self._conn.execute(
                    "ALTER TABLE opportunities_new RENAME TO opportunities")
                # 表随 DROP 一并消失的索引按现有 DDL 重建
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_opp_created "
                    "ON opportunities (created_at DESC)")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_opp_status "
                    "ON opportunities (status)")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_opp_session "
                    "ON opportunities (session_username)")
                self._conn.commit()
            finally:
                self._conn.execute("PRAGMA foreign_keys=ON")
            logger.info("opportunities 表迁移完成（status 支持 handled）")

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
                           confidence: int,
                           direction: str = "") -> dict:
        """插入一条商机并返回完整记录。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO opportunities (session_username, session_name, "
                "sender_wxid, sender_name, window_start, window_end, merged_count, "
                "raw_text, type_id, type_name, summary, key_quote, confidence, "
                "direction, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "datetime('now','localtime'))",
                (session_username, session_name, sender_wxid, sender_name,
                 window_start, window_end, merged_count, raw_text, type_id,
                 type_name, summary, key_quote, confidence,
                 direction if direction in ("buy", "sell") else ""),
            )
            self._conn.commit()
            return self.get_opportunity(cur.lastrowid)  # type: ignore[return-value]

    @staticmethod
    def _opp_row_to_dict(row: sqlite3.Row) -> dict:
        return dict(row)

    def list_opportunities(self, *, page: int = 1, page_size: int = 20,
                           session: str = "", type_id: Optional[int] = None,
                           status: str = "", keyword: str = "",
                           direction: str = "") -> tuple[int, list[dict]]:
        """商机列表（时间倒序）。

        Args:
            status: pending/replied/ignored/handled；空串 = 排除 ignored
                （handled 与 pending/replied 一样默认展示在列表中）。
            direction: buy/sell；空串 = 全部方向（反馈 4）。

        Returns:
            (total, items)
        """
        where: list[str] = []
        params: list[Any] = []
        if status in ("pending", "replied", "ignored", "handled"):
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
        if direction in ("buy", "sell"):
            where.append("direction=?")
            params.append(direction)
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
                "key_quote, confidence, direction, status, created_at "
                f"FROM opportunities WHERE {where_sql} "
                "ORDER BY created_at DESC, id DESC "
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
    def insert_ai_usage(self, *, prompt_tokens: int,
                        completion_tokens: int, model: str = "",
                        session_username: str = "") -> int:
        """写入一条 AI 用量存档（账单数据源，与 analysis_logs 解耦）。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO ai_usage (model, session_username, prompt_tokens, "
                "completion_tokens, created_at) "
                "VALUES (?, ?, ?, ?, datetime('now','localtime'))",
                (model or "", session_username or "",
                 int(prompt_tokens or 0), int(completion_tokens or 0)),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def insert_analysis_log(self, *, session_username: str,
                            sender_wxid: str = "", msg_count: int = 0,
                            input_text: str = "", result: str = "",
                            status: str, error: str = "", model: str = "",
                            latency_ms: Optional[int] = None,
                            opportunity_id: Optional[int] = None,
                            prompt_tokens: Optional[int] = None,
                            completion_tokens: Optional[int] = None,
                            agg_wait_ms: Optional[int] = None,
                            queue_wait_ms: Optional[int] = None,
                            e2e_ms: Optional[int] = None,
                            voice_local_id: Optional[int] = None) -> int:
        """写入一条分析日志（ok/error/skipped/below_threshold）。

        prompt_tokens/completion_tokens：AI 调用 token 用量（反馈 13
        账单估算数据源）；skipped 等未调用 AI 的场景为 None。

        时序三段（诊断 AI 慢在哪）：
        - agg_wait_ms：收到消息 → 合并窗口 flush（第一级窗口等待）；
        - queue_wait_ms：入队 → worker 取出（排队等空闲 worker）；
        - e2e_ms：收到消息 → 本条日志落库（端到端）。
        ``latency_ms`` 仍只表示 AI 接口调用本身的耗时。

        ``voice_local_id``：语音消息在微信会话内的 local_id（非语音消息为
        None）。语音转写失败时靠它做延迟重试回填与前端手动重试。
        """
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO analysis_logs (session_username, sender_wxid, "
                "msg_count, input_text, result, status, error, model, "
                "latency_ms, opportunity_id, prompt_tokens, completion_tokens, "
                "agg_wait_ms, queue_wait_ms, e2e_ms, voice_local_id, "
                "created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "datetime('now','localtime'))",
                (session_username, sender_wxid, msg_count, input_text, result,
                 status, error, model, latency_ms, opportunity_id,
                 prompt_tokens, completion_tokens,
                 agg_wait_ms, queue_wait_ms, e2e_ms,
                 int(voice_local_id) if voice_local_id else None),
            )
            # 同一事务里镜像到 ai_usage：账单只认这张表，即使之后分析日志
            # 被「历史清理」删掉，用量与费用统计也不会出现空洞。
            if prompt_tokens is not None or completion_tokens is not None:
                self._conn.execute(
                    "INSERT INTO ai_usage (model, session_username, "
                    "prompt_tokens, completion_tokens, created_at) "
                    "VALUES (?, ?, ?, ?, datetime('now','localtime'))",
                    (model or "", session_username or "",
                     int(prompt_tokens or 0), int(completion_tokens or 0)),
                )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def update_analysis_log_voice(self, log_id: int, *, input_text: str,
                                  status: str, error: str) -> bool:
        """回填一条语音分析日志（补转写成功 / 重试用尽时更新终态）。

        语音转写失败时会先落一条 ``skipped`` 日志占位（让用户在分析日志里
        看到这条语音的存在与原因），随后由补转写队列异步更新它 —— 不新插
        日志，避免同一条语音在列表里出现两条。

        Args:
            log_id: 待更新的日志 id（``insert_analysis_log`` 的返回值）。
            input_text: 替换后的输入摘要（补转写成功时为 ``[语音] 文本``）。
            status: 新状态（补转写成功 ``ok``；重试用尽仍 ``skipped``）。
            error: 新的原因文案（成功时说明「已补齐并重新分析」）。

        Returns:
            是否更新到了目标行（id 缺失/已被清理时返回 False）。
        """
        if not log_id:
            return False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE analysis_logs SET input_text=?, status=?, error=? "
                "WHERE id=?",
                (str(input_text or ""), str(status or "skipped"),
                 str(error or ""), int(log_id)),
            )
            self._conn.commit()
            return bool(cur.rowcount)

    def list_analysis_logs(self, *, page: int = 1, page_size: int = 20,
                           status: str = "", session: str = "",
                           keyword: str = "") -> tuple[int, list[dict]]:
        """分析日志分页列表（id 倒序 = 时间倒序）。

        Args:
            status: ok/error/skipped/below_threshold；空串 = 全部。
            session: 会话筛选（session_username 模糊匹配）。
            keyword: 关键词筛选（input_text 模糊匹配）。

        Returns:
            (total, items)
        """
        where: list[str] = []
        params: list[Any] = []
        if status in ("ok", "error", "skipped", "below_threshold"):
            where.append("status=?")
            params.append(status)
        if session:
            where.append("session_username LIKE ?")
            params.append(f"%{session}%")
        if keyword:
            where.append("input_text LIKE ?")
            params.append(f"%{keyword}%")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM analysis_logs{where_sql}", params
            ).fetchone()[0]
            rows = self._conn.execute(
                "SELECT id, created_at, session_username, sender_wxid, "
                "msg_count, input_text, result, status, error, model, "
                "latency_ms, agg_wait_ms, queue_wait_ms, e2e_ms, "
                "voice_local_id "
                "FROM analysis_logs"
                f"{where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (max(page, 1) - 1) * page_size],
            ).fetchall()
        return int(total), [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 历史数据管理（反馈 7）
    # ------------------------------------------------------------------
    _PURGE_TABLES = ("analysis_logs", "opportunities")
    # 注意：ai_usage（账单数据源）刻意不在清理白名单里 —— 无论用户怎么
    # 清理日志/商机，AI 用量与费用历史都必须保留完整。
    # 同理 voice_texts（语音转写缓存）也不可清理：它是纯缓存，删了只会让
    # 同一条语音被重复识别，不产生任何收益。

    # 截断点表达式：由 SQLite 自己在本地时间下按自然日计算，格式与落库值
    # （datetime('now','localtime')）完全一致。
    # 绝不能用 Python 拼时间字符串做比较：isoformat() 产生 'T' 分隔
    # （2026-11-10T11:00:53），而落库值是空格分隔（2026-11-10 23:32:22），
    # SQLite 按 memcmp 比较 TEXT 时 ' '(0x20) < 'T'(0x54)，会把「与截断点
    # 同一天」的记录全部误判为更早而删掉（曾导致保留 1 天时丢一整天的数据）。
    _PURGE_CUTOFF_SQL = "datetime('now','localtime','start of day', ?)"

    def _purge_modifier(self, days: int) -> str:
        """校验保留天数并返回 SQLite 时间修饰符。

        语义「保留最近 N 个自然日（含今天）」→ 截断点为 (今天 - (N-1)) 00:00:00，
        因此修饰符是 ``-(N-1) days``；N=1 时为 ``'-0 days'``（SQLite 合法，
        等价于今天 00:00:00，只保留今天）。
        """
        if days < 1:
            raise ValueError("保留天数必须 ≥ 1")
        return f"-{int(days) - 1} days"

    def purge_cutoff(self, days: int) -> str:
        """清理截断点（YYYY-MM-DD HH:MM:SS，本地时间，按自然日对齐）。

        语义：保留最近 ``days`` 个自然日（含今天），即删除
        (今天 - (days-1)) 00:00:00 之前的记录。例如：
        - days=1 → 截断点「今天 00:00:00」，只保留今天（删昨天及更早）；
        - days=2 → 截断点「昨天 00:00:00」，昨天与今天完整保留；
        - days=30 → 截断点「29 天前 00:00:00」，正好 30 个自然日。
        该值同时用于清理预览、真删与前端确认提示，保证三处口径一致。
        """
        modifier = self._purge_modifier(days)
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._PURGE_CUTOFF_SQL}", (modifier,)).fetchone()
        return str(row[0]) if row and row[0] is not None else ""

    def count_older_than(self, table: str, days: int) -> int:
        """统计指定表将被清理的记录条数（预览用，不做任何删除）。

        Args:
            table: 仅允许 analysis_logs / opportunities（白名单防注入）。
            days: 保留最近 N 个自然日（含今天，≥1）。
        """
        if table not in self._PURGE_TABLES:
            raise ValueError(f"不允许清理的表：{table}")
        modifier = self._purge_modifier(days)
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE created_at < "
                f"{self._PURGE_CUTOFF_SQL}", (modifier,)).fetchone()
        return int(row[0]) if row else 0

    def delete_older_than(self, table: str, days: int) -> int:
        """删除指定表创建时间早于截断点的记录，返回删除行数。

        Args:
            table: 仅允许 analysis_logs / opportunities（白名单防注入）。
            days: 保留最近 N 个自然日（含今天，≥1）；删除截断点之前的记录。

        截断点由 SQLite 按本地时间计算（见 _PURGE_CUTOFF_SQL），与 created_at
        落库格式一致，避免字符串格式差异造成的「差一天」误删。
        opportunities 删除前先把 analysis_logs.opportunity_id 引用置空，
        避免外键约束阻止删除（日志保留完整，仅断开关联）。
        """
        if table not in self._PURGE_TABLES:
            raise ValueError(f"不允许清理的表：{table}")
        modifier = self._purge_modifier(days)
        with self._lock:
            if table == "opportunities":
                self._conn.execute(
                    "UPDATE analysis_logs SET opportunity_id=NULL "
                    "WHERE opportunity_id IN ("
                    "SELECT id FROM opportunities WHERE created_at < "
                    f"{self._PURGE_CUTOFF_SQL})",
                    (modifier,),
                )
            cur = self._conn.execute(
                f"DELETE FROM {table} WHERE created_at < "
                f"{self._PURGE_CUTOFF_SQL}", (modifier,))
            self._conn.commit()
            return int(cur.rowcount)

    # ------------------------------------------------------------------
    # AI 账单估算（反馈 13）
    # ------------------------------------------------------------------
    def usage_by_date(self, date_str: str) -> dict:
        """某日（YYYY-MM-DD）的 AI token 用量与调用次数汇总。

        数据源是 ai_usage 独立存档（不是 analysis_logs）：日志被手工清理
        后账单历史仍然完整。命中但低于置信度门槛（below_threshold）的调用
        同样消耗 token，也计入这里。

        Returns:
            {calls, prompt_tokens, completion_tokens}（无数据为 0）。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS calls, "
                "COALESCE(SUM(prompt_tokens), 0) AS pt, "
                "COALESCE(SUM(completion_tokens), 0) AS ct "
                "FROM ai_usage WHERE substr(created_at, 1, 10) = ?",
                (date_str,),
            ).fetchone()
        return {"calls": int(row["calls"]), "prompt_tokens": int(row["pt"]),
                "completion_tokens": int(row["ct"])}

    def usage_series(self, days: int = 7) -> list[dict]:
        """近 N 日（含今日）逐日 token 用量序列（账单卡片展示）。"""
        out: list[dict] = []
        for i in range(days - 1, -1, -1):
            day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            usage = self.usage_by_date(day)
            out.append({"date": day, **usage})
        return out

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
    # 群名片学习缓存 DAO（group_cards，批次 5 需求 ②）
    # ------------------------------------------------------------------
    def get_group_cards(self, chatroom: str) -> dict[str, str]:
        """读取一个群已学习到的成员微信端显示名（username → 群名片）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT member, card FROM group_cards WHERE chatroom=?",
                (str(chatroom or ""),),
            ).fetchall()
        return {r["member"]: r["card"] for r in rows if r["card"]}

    def save_group_card(self, chatroom: str, member: str, card: str) -> None:
        """写入/覆盖一条群名片学习结果（INSERT OR REPLACE）。

        注意：名片可能随时被成员改掉，但新消息到达时会持续覆盖更新，
        最终一致；不做 TTL 失效（名片过期只影响显示，不影响判定）。
        """
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO group_cards "
                "(chatroom, member, card, updated_at) VALUES (?, ?, ?, ?)",
                (str(chatroom or ""), str(member or ""), str(card or ""),
                 now_iso()),
            )
            self._conn.commit()

    def delete_group_cards(self, chatroom: str) -> int:
        """删除一个群的全部学习名片（成员退出群等场景预留）。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM group_cards WHERE chatroom=?", (str(chatroom or ""),)
            )
            self._conn.commit()
            return int(cur.rowcount)

    # ------------------------------------------------------------------
    # 语音转写缓存 DAO（voice_texts）
    # ------------------------------------------------------------------
    def get_voice_text(self, username: str, local_id: int) -> Optional[dict]:
        """取单条语音的转写缓存；无缓存返回 None。

        Returns:
            {username, local_id, text, status, error, duration_ms, created_at}
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT username, local_id, text, status, error, duration_ms, "
                "created_at FROM voice_texts "
                "WHERE username=? AND local_id=?",
                (str(username or ""), int(local_id)),
            ).fetchone()
        return dict(row) if row else None

    def save_voice_text(self, username: str, local_id: int, *,
                        text: str, status: str, error: str = "",
                        duration_ms: int = 0) -> None:
        """写入/覆盖一条语音转写结果（INSERT OR REPLACE，主键 username+local_id）。"""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO voice_texts "
                "(username, local_id, text, status, error, duration_ms, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(username or ""), int(local_id), str(text or ""),
                 str(status or ""), str(error or ""), int(duration_ms or 0),
                 now_iso()),
            )
            self._conn.commit()

    def get_voice_texts(self, username: str,
                        local_ids: list[int]) -> dict[int, dict]:
        """批量取多条语音的转写缓存（避免消息列表逐条查询 N 次）。

        Args:
            username: 会话 username。
            local_ids: 消息 local_id 列表（空列表直接返回 ``{}``）。

        Returns:
            {local_id: 缓存行 dict}，未命中的 local_id 不出现在结果里。
        """
        ids: list[int] = []
        for raw in local_ids or []:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                ids.append(value)
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                "SELECT username, local_id, text, status, error, duration_ms, "
                "created_at FROM voice_texts "
                f"WHERE username=? AND local_id IN ({placeholders})",
                [str(username or ""), *ids],
            ).fetchall()
        return {int(r["local_id"]): dict(r) for r in rows}

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
    """读取 AI 接口配置（api_key_enc 保持密文，由调用方解密）。

    disable_thinking：是否关闭模型思考模式（默认 "1"）。思考型模型默认会
    把隐藏推理 token 计入 completion，单条判定从 3 秒级放大到 30~60 秒。
    enabled：是否使用 AI 接口（默认 "1"）。为 "0" 时管线不调用 AI，
    改用「轻量筛选」（预筛关键词 / 正则）直接判定商机。
    """
    return {
        "base_url": db.get_setting(KEY_AI_BASE_URL),
        "api_key_enc": db.get_setting("ai.api_key_enc"),
        "model": db.get_setting(KEY_AI_MODEL),
        "timeout_sec": db.get_setting(KEY_AI_TIMEOUT_SEC, "60"),
        "temperature": db.get_setting(KEY_AI_TEMPERATURE, "0.2"),
        "disable_thinking": db.get_setting("ai.disable_thinking", "1"),
        "enabled": db.get_setting(KEY_AI_ENABLED, "1"),
    }


def read_correction_examples(db: Database) -> list[str]:
    """读取分析修正示例（analysis.correction_examples，JSON 数组字符串）。

    脏数据防御：解析失败/类型异常一律回退空列表，不崩管线。
    """
    import json as _json

    raw = db.get_setting(KEY_ANALYSIS_CORRECTION_EXAMPLES, "[]")
    try:
        data = _json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(e) for e in data if str(e).strip()]


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
        "correction_examples": read_correction_examples(db),
        "compliance_confirmed": db.get_bool(KEY_COMPLIANCE_CONFIRMED),
        # 过滤自己发送的消息（默认 True：自己发的消息不进入商机分析）
        "filter_self_messages": db.get_setting(KEY_ANALYSIS_FILTER_SELF, "1") != "0",
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
