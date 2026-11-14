# -*- coding: utf-8 -*-
"""启动钩子：建表 → 首启管理员 → 清理过期会话 → 打印自检信息。

由 FastAPI ``lifespan`` 调用（``python run.py init-db`` 只调用 ``init_db``）。
"""
from __future__ import annotations

import logging
import secrets
from typing import Any, Dict, Optional

from app import db, security, signing
from app.config import DEFAULT_DB_PASSWORD, get_config
from app.repo import admins, audit

logger = logging.getLogger(__name__)


def init_db() -> int:
    """执行 ``app/schema.sql``（幂等，可重复执行），返回语句条数。"""
    cfg = get_config()
    count = db.execute_script(cfg.schema_path)
    logger.info("建表脚本执行完成：%d 条语句（%s）", count, cfg.dsn)
    _migrate_code_voided()
    return count


def _migrate_code_voided() -> None:
    """给已存在的库补 ``license_code.voided_at`` 列（幂等迁移）。

    ``CREATE TABLE IF NOT EXISTS`` 不会给**已存在**的表加新列，所以老库升级
    依赖这里：查 ``information_schema``，缺列才 ``ALTER TABLE``。
    """
    with db.transaction() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'license_code' "
            "AND COLUMN_NAME = 'voided_at'")
        row = cur.fetchone() or {}
        if int(row.get("n") or 0) > 0:
            return
        cur.execute(
            "ALTER TABLE license_code ADD COLUMN voided_at DATETIME NULL "
            "DEFAULT NULL COMMENT '非 NULL=已作废（仅未使用的码可作废）' "
            "AFTER used_at, ADD KEY idx_voided (voided_at)")
        logger.info("已迁移：license_code 新增 voided_at 列（激活码作废功能）")


def bootstrap_admin(verbose: bool = True) -> Dict[str, Any]:
    """首次启动时创建管理员账号。

    * ``LS_BOOTSTRAP_ADMIN_PASSWORD`` 有值 → 直接用它（``must_change_password=0``）；
    * 为空 → 随机生成 16 位密码**打印到控制台**并置 ``must_change_password=1``
      （该状态下除改密/登出/me 外接口一律 403）。

    Returns:
        ``{"created": bool, "username": str, "password": str|None,
        "must_change": bool}``。
    """
    cfg = get_config()
    moment = signing.now()
    with db.transaction() as cur:
        total = admins.count_all(cur)
        if total > 0:
            return {"created": False, "username": "", "password": None,
                    "must_change": False, "total": total}

        password = str(cfg.bootstrap_admin_password or "").strip()
        generated = False
        if not password:
            password = secrets.token_urlsafe(12)        # 16 个字符
            generated = True
        username = cfg.bootstrap_admin_user.strip().lower()[:32] or "admin"
        password_hash = security.hash_password(password)
        admins.create(cur, username=username, password_hash=password_hash,
                      display_name=cfg.bootstrap_admin_display,
                      must_change_password=generated, now=moment)
        audit.write(cur, now=moment, actor="system", actor_id=0,
                    action="bootstrap_admin", target_type="admin",
                    target_id=username, note="首启自动创建管理员账号")

    if verbose:
        print("=" * 70, flush=True)
        print("  首次启动：已创建管理员账号", flush=True)
        print(f"  用户名：{username}", flush=True)
        if generated:
            print(f"  初始密码：{password}", flush=True)
            print("  （随机生成，首次登录后必须修改密码；本行只打印这一次）",
                  flush=True)
        else:
            print("  初始密码：取自 LS_BOOTSTRAP_ADMIN_PASSWORD", flush=True)
        print("=" * 70, flush=True)
    logger.warning("已创建管理员 %s（%s）", username,
                   "随机密码" if generated else "配置密码")
    return {"created": True, "username": username, "password": password,
            "must_change": generated, "total": 1}


def cleanup_sessions() -> int:
    """清理过期/已吊销会话（登录表很大时避免累积）。"""
    moment = signing.now()
    try:
        with db.transaction() as cur:
            removed = admins.cleanup_expired(cur, moment)
        if removed:
            logger.info("已清理 %d 条过期会话", removed)
        return removed
    except Exception as exc:                        # 清理失败不应阻塞启动
        logger.warning("清理过期会话失败（忽略）：%s", exc)
        return 0


def print_banner(verbose: bool = True) -> Dict[str, Any]:
    """打印启动自检信息（地址 / key_id / 公钥 base64 / 数据源，无口令）。"""
    cfg = get_config()
    try:
        private_key = signing.get_private_key()
        public_b64 = signing.public_key_b64(private_key)
    except SystemExit:
        raise
    except Exception as exc:                        # 私钥异常不静默
        logger.error("加载签名私钥失败：%s", exc)
        raise
    info = {
        "key_id": cfg.key_id,
        "private_key_path": str(cfg.private_key_path),
        "public_key_b64": public_b64,
        "dsn": cfg.dsn,
        "listen": f"http://{cfg.host}:{cfg.port}",
    }
    if verbose:
        print("-" * 70, flush=True)
        print(f"  授权服务后台 · 监听 {info['listen']}   管理台 {info['listen']}/admin/",
              flush=True)
        print(f"  数据库：{info['dsn']}（口令不打印）", flush=True)
        print(f"  签名 key_id：{info['key_id']}", flush=True)
        print(f"  私钥路径：{info['private_key_path']}", flush=True)
        print(f"  公钥(base64)：{public_b64}", flush=True)
        print("  ↑ 该公钥必须与客户端 app/license.py 的 PUBLIC_KEYS 一致，否则验签必失败",
              flush=True)
        print("-" * 70, flush=True)
        if cfg.db_password == DEFAULT_DB_PASSWORD:
            print("  WARNING: 正在使用默认数据库口令，生产环境必须覆盖（LS_DB_PASSWORD）",
                  flush=True)
        if not cfg.cookie_secure:
            print("  WARNING: COOKIE_SECURE=0（仅适用于本地 http 联调；"
                  "HTTPS 部署必须设为 1）", flush=True)
    return info


def run_startup(verbose: bool = True) -> Dict[str, Any]:
    """完整启动流程（幂等）：建表 → 建号 → 清会话 → 打印自检。"""
    db.init_pool()
    statements = init_db()
    admin_info = bootstrap_admin(verbose=verbose)
    cleanup_sessions()
    banner = print_banner(verbose=verbose)
    tables = []
    try:
        tables = db.list_tables()
    except Exception as exc:
        logger.warning("读取表清单失败：%s", exc)
    logger.info("启动完成：表 %d 张 %s", len(tables), tables)
    return {"statements": statements, "admin": admin_info, "banner": banner,
            "tables": tables}


def describe_tables() -> Optional[Dict[str, Any]]:
    """返回表清单与关键索引（``run.py init-db`` 的回显用）。"""
    try:
        tables = db.list_tables()
        row = db.query_one("SHOW CREATE TABLE license")
        create_sql = ""
        if row:
            for key in ("Create Table", "Create View"):
                if key in row:
                    create_sql = str(row[key])
                    break
        return {"tables": tables,
                "has_current_key_index":
                    "uk_current_machine" in create_sql}
    except Exception as exc:
        logger.error("读取表结构失败：%s", exc)
        return None
