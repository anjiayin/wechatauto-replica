# -*- coding: utf-8 -*-
"""配置层：进程环境变量 > .env 文件 > 代码默认值。

不引入 python-dotenv：``_read_env_file`` 只有 20 行，规则够用
（``KEY=VALUE``、``#`` 注释、空行、成对引号、可选 ``export`` 前缀）。

变量名兼容两套写法（带 ``LS_`` 前缀的优先），例如
``LS_DB_HOST`` / ``DB_HOST``、``LS_KEY_ID`` / ``LICENSE_KEY_ID``，
便于部署脚本统一加前缀，同时兼容设计文档里的原始变量名。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# 项目根目录（license-server/）
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
ENV_FILE: Path = PROJECT_ROOT / ".env"

#: 本机开发默认数据库口令（仅为了「零配置可直接跑通」；生产必须覆盖）
DEFAULT_DB_PASSWORD = "dataaudit1234"


# ---------------------------------------------------------------------------
# .env 解析
# ---------------------------------------------------------------------------
def _read_env_file(path: Path) -> dict[str, str]:
    """解析 ``KEY=VALUE`` 形式的 .env 文件；文件不存在时返回空字典。"""
    data: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return data
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            data[key] = value
    return data


_ENV_CACHE: Optional[dict[str, str]] = None


def _env_file_values() -> dict[str, str]:
    """读取并缓存 .env（进程内只读一次）。"""
    global _ENV_CACHE
    if _ENV_CACHE is None:
        _ENV_CACHE = _read_env_file(ENV_FILE)
    return _ENV_CACHE


def _first(*names: str) -> str:
    """按顺序取第一个「非空」的配置值（环境变量优先，其次 .env）。"""
    for name in names:
        value = os.environ.get(name)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    file_values = _env_file_values()
    for name in names:
        value = file_values.get(name)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return ""


def _get_str(names: tuple[str, ...], default: str) -> str:
    """取字符串配置；都没有时用默认值。"""
    value = _first(*names)
    return value if value != "" else default


def _get_int(names: tuple[str, ...], default: int, minimum: Optional[int] = None,
             maximum: Optional[int] = None) -> int:
    """取整数配置；非法值退回默认值并按需夹取上下界。"""
    raw = _first(*names)
    if raw == "":
        value = default
    else:
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            value = default
    if minimum is not None and value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return value


def _get_bool(names: tuple[str, ...], default: bool) -> bool:
    """取布尔配置（1/true/yes/on 为真，0/false/no/off 为假）。"""
    raw = _first(*names).lower()
    if raw == "":
        return default
    if raw in ("1", "true", "yes", "on", "y"):
        return True
    if raw in ("0", "false", "no", "off", "n"):
        return False
    return default


def _get_rate(names: tuple[str, ...], default: tuple[int, int]) -> tuple[int, int]:
    """取限流配置 ``次数/秒数``；格式非法时退回默认值。"""
    raw = _first(*names)
    if raw == "":
        return default
    text = raw.replace(" ", "")
    if "/" not in text:
        return default
    head, tail = text.split("/", 1)
    try:
        limit, window = int(head), int(tail)
    except ValueError:
        return default
    if limit <= 0 or window <= 0:
        return default
    return limit, window


# ---------------------------------------------------------------------------
# 配置对象
# ---------------------------------------------------------------------------
class Config:
    """全局配置（进程内单例）。"""

    def __init__(self) -> None:
        # -- HTTP 服务 --
        self.host: str = _get_str(("LS_HOST", "HOST"), "127.0.0.1")
        self.port: int = _get_int(("LS_PORT", "PORT"), 8800, 1, 65535)
        self.enable_docs: bool = _get_bool(("LS_ENABLE_DOCS", "ENABLE_DOCS"), True)
        self.trust_proxy: bool = _get_bool(("LS_TRUST_PROXY", "TRUST_PROXY"), True)

        # -- 数据库 --
        self.db_host: str = _get_str(("LS_DB_HOST", "DB_HOST"), "192.168.1.172")
        self.db_port: int = _get_int(("LS_DB_PORT", "DB_PORT"), 3306, 1, 65535)
        self.db_user: str = _get_str(("LS_DB_USER", "DB_USER"), "dataaudit")
        self.db_password: str = _get_str(
            ("LS_DB_PASSWORD", "DB_PASSWORD"), DEFAULT_DB_PASSWORD)
        self.db_name: str = _get_str(("LS_DB_NAME", "DB_NAME"), "wechat_kefu")
        self.db_charset: str = _get_str(("LS_DB_CHARSET", "DB_CHARSET"), "utf8mb4")
        self.db_pool_size: int = _get_int(
            ("LS_DB_POOL_SIZE", "DB_POOL_SIZE"), 10, 1, 200)
        self.db_connect_timeout: int = _get_int(
            ("LS_DB_CONNECT_TIMEOUT", "DB_CONNECT_TIMEOUT"), 5, 1, 60)

        # -- 签名密钥 --
        self.key_id: str = _get_str(("LS_KEY_ID", "LICENSE_KEY_ID"), "dev")
        self._private_key_raw: str = _get_str(
            ("LS_PRIVATE_KEY_PATH", "LICENSE_PRIVATE_KEY_PATH"),
            "../kefu-system/tools/license_keys/dev_private.pem")

        # -- 授权默认值 --
        self.grace_days: int = _get_int(
            ("LS_DEFAULT_GRACE_DAYS", "DEFAULT_GRACE_DAYS"), 7, 0, 365)
        self.default_edition: str = _get_str(
            ("LS_DEFAULT_EDITION", "DEFAULT_EDITION"), "standard")
        self.default_code_days: int = _get_int(
            ("LS_DEFAULT_CODE_DAYS", "DEFAULT_CODE_DAYS"), 365, 1, 3650)
        self.features_default: str = _get_str(
            ("LS_FEATURES_DEFAULT", "FEATURES_DEFAULT"), "monitor,reply,ai")

        # -- 会话与安全 --
        self.session_ttl_hours: int = _get_int(
            ("LS_SESSION_TTL_HOURS", "SESSION_TTL_HOURS"), 12, 1, 24 * 30)
        self.session_max_days: int = _get_int(
            ("LS_SESSION_MAX_DAYS", "SESSION_MAX_DAYS"), 7, 1, 365)
        self.cookie_name: str = _get_str(
            ("LS_COOKIE_NAME", "COOKIE_NAME"), "ls_session")
        self.cookie_secure: bool = _get_bool(
            ("LS_COOKIE_SECURE", "COOKIE_SECURE"), False)
        self.pbkdf2_iterations: int = _get_int(
            ("LS_PBKDF2_ITERATIONS", "PBKDF2_ITERATIONS"), 260000, 100000, 2000000)
        self.login_max_failed: int = _get_int(
            ("LS_LOGIN_MAX_FAILED", "LOGIN_MAX_FAILED"), 5, 1, 100)
        self.login_lock_minutes: int = _get_int(
            ("LS_LOGIN_LOCK_MINUTES", "LOGIN_LOCK_MINUTES"), 15, 1, 1440)
        self.login_rate_limit: tuple[int, int] = _get_rate(
            ("LS_LOGIN_RATE_LIMIT", "LOGIN_RATE_LIMIT"), (20, 60))
        self.activate_rate_limit: tuple[int, int] = _get_rate(
            ("LS_ACTIVATE_RATE_LIMIT", "ACTIVATE_RATE_LIMIT"), (10, 60))
        self.query_rate_limit: tuple[int, int] = _get_rate(
            ("LS_QUERY_RATE_LIMIT", "QUERY_RATE_LIMIT"), (30, 60))

        # -- 首启管理员 --
        self.bootstrap_admin_user: str = _get_str(
            ("LS_BOOTSTRAP_ADMIN_USER", "BOOTSTRAP_ADMIN_USER"), "admin")
        self.bootstrap_admin_password: str = _first(
            "LS_BOOTSTRAP_ADMIN_PASSWORD", "BOOTSTRAP_ADMIN_PASSWORD")
        self.bootstrap_admin_display: str = _get_str(
            ("LS_BOOTSTRAP_ADMIN_DISPLAY", "BOOTSTRAP_ADMIN_DISPLAY"), "系统管理员")

    # -- 派生属性 ----------------------------------------------------
    @property
    def private_key_path(self) -> Path:
        """私钥路径（相对路径按**项目根**解析）。"""
        path = Path(self._private_key_raw).expanduser()
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()

    @property
    def static_dir(self) -> Path:
        """管理台静态文件目录。"""
        return PROJECT_ROOT / "app" / "static"

    @property
    def schema_path(self) -> Path:
        """建表 SQL。"""
        return PROJECT_ROOT / "app" / "schema.sql"

    @property
    def dsn(self) -> str:
        """日志用的数据源描述（**不含口令**）。"""
        return f"{self.db_user}@{self.db_host}:{self.db_port}/{self.db_name}"


_CONFIG: Optional[Config] = None


def get_config() -> Config:
    """取配置单例（首次调用时读取 env 与 .env）。"""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = Config()
    return _CONFIG


def reset_config() -> None:
    """清空配置与 .env 缓存（测试用）。"""
    global _CONFIG, _ENV_CACHE
    _CONFIG = None
    _ENV_CACHE = None
