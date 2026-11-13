# -*- coding: utf-8 -*-
"""kefu-system 启动入口。

用法（在 kefu-system 目录下）::

    python run.py                      # 默认 127.0.0.1:8765
    python run.py --port 9000          # 命令行参数优先（仅本次启动生效）
    python run.py --port 9000 --save   # 保存端口到配置库，之后直接 python run.py 即用 9000
    KEFU_PORT=9000 python run.py       # 其次读环境变量
    python run.py --port 8765 --save   # 恢复默认端口（把 8765 写回配置库）

监听地址优先级：**命令行参数 > 环境变量 > 已保存配置 > 默认值**。
默认仍是 127.0.0.1:8765（前端 dev 代理也默认指向该端口，不改默认行为）。

``--save`` 会把本次**最终解析出**的 host 与 port 写入配置库（settings 表的
``run.host`` / ``run.port`` 键），下次启动不再需要重复指定；不带 ``--save``
时端口只对本次启动生效。注意：配置库由系统首次正常启动时创建，若尚未启动过
（配置库不存在），``--save`` 会拒绝保存而不是替你建库。

环境变量：
- ``KEFU_HOST``：监听地址（默认 127.0.0.1）
- ``KEFU_PORT``：监听端口（默认 8765，合法范围 1~65535）

前端联调提示：前端 dev 模式的代理 target 读取 ``KEFU_PORT`` 环境变量；若
持久化端口 ≠ 8765，前端开发时需同步 ``set KEFU_PORT=...``；生产为后端
同源托管，不受影响。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Callable, NamedTuple

import uvicorn

from app.config import DB_PATH

# 默认监听地址（**不得改变**：前端 dev 代理与生产静态托管均默认指向它）
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

ENV_HOST = "KEFU_HOST"
ENV_PORT = "KEFU_PORT"

# settings 表配置键（与既有 ai.* / analysis.* 命名风格一致）
SETTING_KEY_HOST = "run.host"
SETTING_KEY_PORT = "run.port"

# 端口/地址来源文案（用于启动日志，让人一眼看出端口从哪来）
ORIGIN_CLI = "命令行参数"
ORIGIN_ENV = "环境变量"
ORIGIN_SAVED = "已保存配置"
ORIGIN_DEFAULT = "默认值"


class ResolvedAddress(NamedTuple):
    """一次地址解析的完整结果（含来源标注，供启动日志与 --save 使用）。"""

    host: str
    port: int
    host_origin: str
    port_origin: str
    save: bool


def _parse_port(value: object, origin: str) -> int:
    """把端口取值解析成合法端口，非法时以退出码 2 结束（中文提示）。

    Args:
        value: 待解析值（命令行字符串 / 环境变量字符串 / 已保存配置 / 默认整数）。
        origin: 取值来源的中文描述（用于错误提示，如「命令行参数 --port」）。

    Returns:
        1~65535 之间的端口号。
    """
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        print(f"错误：{origin} 不是合法整数：{value!r}", file=sys.stderr)
        print(f"端口需为 1~65535 之间的整数，当前默认 {DEFAULT_PORT}。",
              file=sys.stderr)
        raise SystemExit(2)
    if not 1 <= port <= 65535:
        print(f"错误：{origin} 超出范围：{port}（需为 1~65535）", file=sys.stderr)
        print(f"端口需为 1~65535 之间的整数，当前默认 {DEFAULT_PORT}。",
              file=sys.stderr)
        raise SystemExit(2)
    return port


def _build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器（--help 文案与模块 docstring 保持同步）。"""
    parser = argparse.ArgumentParser(
        description=(
            "kefu-system 客服系统（默认监听 127.0.0.1:8765；地址优先级："
            "命令行 > 环境变量 > 已保存配置 > 默认值）"))
    parser.add_argument(
        "--host", default=None,
        help=f"监听地址（默认 {DEFAULT_HOST}，也可用环境变量 {ENV_HOST}）")
    parser.add_argument(
        "--port", default=None,
        help=f"监听端口（默认 {DEFAULT_PORT}，也可用环境变量 {ENV_PORT}）")
    parser.add_argument(
        "--save", action="store_true",
        help="把本次最终解析出的监听地址保存到配置库（run.host/run.port），"
             "下次启动直接生效；恢复默认用 --port 8765 --save")
    return parser


def _resolve_address_detail(
    argv: list[str] | None = None,
    settings_reader: Callable[[str], str] | None = None,
) -> ResolvedAddress:
    """按「命令行 > 环境变量 > 已保存配置 > 默认」解析监听地址（含来源）。

    Args:
        argv: 命令行参数（不含程序名）；None = 取 ``sys.argv[1:]``。
        settings_reader: 从配置库读值的函数（key → value）；None = 不读库
            （兼容既有测试与外部构造），此时优先级退化为
            「命令行 > 环境变量 > 默认」。

    Returns:
        :class:`ResolvedAddress`，host/port 之外还带来源文案与 --save 标记。
    """
    args = _build_parser().parse_args(argv)

    # 端口：命令行 > 环境变量 > 已保存配置 > 默认
    port: int
    if args.port is not None and str(args.port).strip() != "":
        port = _parse_port(args.port, "命令行参数 --port")
        port_origin = ORIGIN_CLI
    else:
        env_port = os.environ.get(ENV_PORT, "").strip()
        if env_port:
            port = _parse_port(env_port, f"环境变量 {ENV_PORT}")
            port_origin = ORIGIN_ENV
        else:
            saved_port = ""
            if settings_reader is not None:
                # 配置缺失/损坏由 reader 端容错（返回空串），这里只解析值
                saved_port = str(settings_reader(SETTING_KEY_PORT) or "").strip()
            if saved_port:
                # 损坏值同样报错退出，提示里注明来源「已保存配置」
                port = _parse_port(saved_port, "已保存配置 run.port")
                port_origin = ORIGIN_SAVED
            else:
                port = DEFAULT_PORT
                port_origin = ORIGIN_DEFAULT

    # 地址：命令行 > 环境变量 > 已保存配置 > 默认（不做格式校验，交给 uvicorn）
    if args.host is not None and str(args.host).strip() != "":
        host = str(args.host).strip()
        host_origin = ORIGIN_CLI
    else:
        env_host = os.environ.get(ENV_HOST, "").strip()
        saved_host = ""
        if not env_host and settings_reader is not None:
            saved_host = str(settings_reader(SETTING_KEY_HOST) or "").strip()
        if env_host:
            host = env_host
            host_origin = ORIGIN_ENV
        elif saved_host:
            host = saved_host
            host_origin = ORIGIN_SAVED
        else:
            host = DEFAULT_HOST
            host_origin = ORIGIN_DEFAULT
    return ResolvedAddress(
        host=host, port=port,
        host_origin=host_origin, port_origin=port_origin,
        save=bool(args.save))


def resolve_address(
    argv: list[str] | None = None,
    settings_reader: Callable[[str], str] | None = None,
) -> tuple[str, int]:
    """按「命令行 > 环境变量 > 已保存配置 > 默认」解析监听地址。

    Args:
        argv: 命令行参数（不含程序名）；None = 取 ``sys.argv[1:]``。
        settings_reader: 从配置库读值的函数；None = 不读库（默认，行为与
            旧版「命令行 > 环境变量 > 默认」完全一致）。

    Returns:
        ``(host, port)``。
    """
    resolved = _resolve_address_detail(argv, settings_reader)
    return resolved.host, resolved.port


def save_address(host: str, port: int, db_path: Path | None = None) -> None:
    """把最终解析出的监听地址写入配置库（settings 表），失败以退出码 2 结束。

    独立成小函数并支持注入 ``db_path``，便于测试；不经过 ``app.db.Database``
    （那里会跑全套迁移，启动入口要保持轻量）。

    Args:
        host: 要保存的监听地址。
        port: 要保存的监听端口。
        db_path: 配置库路径；None = 使用 ``app.config.DB_PATH``。

    Raises:
        SystemExit: 配置库不存在（系统尚未启动过）或写入失败时，退出码 2。
    """
    path = Path(db_path) if db_path is not None else DB_PATH
    if not path.exists():
        # 不要替 Database 建库：避免出现半初始化状态
        print("错误：配置库不存在（系统可能尚未启动过），无法保存。",
              file=sys.stderr)
        print("请先正常启动一次系统后，再使用 --save 保存监听地址。",
              file=sys.stderr)
        raise SystemExit(2)
    try:
        conn = sqlite3.connect(str(path))
        try:
            with conn:      # 事务：两条配置要么都写入，要么都不写
                conn.execute(
                    "INSERT INTO settings (key, value, updated_at) "
                    "VALUES (?, ?, datetime('now','localtime')) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (SETTING_KEY_HOST, host))
                conn.execute(
                    "INSERT INTO settings (key, value, updated_at) "
                    "VALUES (?, ?, datetime('now','localtime')) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (SETTING_KEY_PORT, str(port)))
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"错误：保存监听地址失败：{exc}", file=sys.stderr)
        raise SystemExit(2)
    print(f"已保存监听地址 http://{host}:{port}，下次启动生效。")


def _read_saved_setting(key: str) -> str:
    """从配置库只读读取一个 settings 值；任何缺失/失败都返回空串，不挡启动。

    只用 ``sqlite3`` 做只读查询，不 import ``app.db.Database``（其初始化会
    跑全套迁移，启动入口要保持轻量）。库文件不存在 / 表不存在 / 表锁 /
    权限问题时一律返回空串，继续用默认值。
    """
    path = Path(DB_PATH)
    if not path.exists():
        return ""
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return ""
    return str(row[0]) if row else ""


def _format_origin_note(resolved: ResolvedAddress) -> str:
    """拼装启动日志里的来源标注（端口必标；host 仅在非默认时标注）。"""
    parts = [f"端口来源：{resolved.port_origin}"]
    if resolved.host != DEFAULT_HOST:
        parts.append(f"监听地址来源：{resolved.host_origin}")
    return f"（{'；'.join(parts)}）"


def main() -> None:
    """启动 FastAPI 应用（单进程，兼容 GUI 自动化线程模型）。"""
    resolved = _resolve_address_detail(
        settings_reader=_read_saved_setting)
    if resolved.save:
        # --save：把最终解析出的 host 与 port（无论来自哪一级）写入配置库
        save_address(resolved.host, resolved.port)
    note = _format_origin_note(resolved)
    print(f"kefu-system 正在启动，"
          f"监听地址：http://{resolved.host}:{resolved.port}{note}")
    uvicorn.run(
        "app.main:app",
        host=resolved.host,
        port=resolved.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
