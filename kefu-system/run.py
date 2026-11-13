# -*- coding: utf-8 -*-
"""kefu-system 启动入口。

用法（在 kefu-system 目录下）::

    python run.py                      # 默认 127.0.0.1:8765
    python run.py --port 9000          # 命令行参数优先
    KEFU_PORT=9000 python run.py       # 其次读环境变量

监听地址优先级：**命令行参数 > 环境变量 > 默认值**。
默认仍是 127.0.0.1:8765（前端 dev 代理也默认指向该端口，不改默认行为）。

环境变量：
- ``KEFU_HOST``：监听地址（默认 127.0.0.1）
- ``KEFU_PORT``：监听端口（默认 8765，合法范围 1~65535）
"""
from __future__ import annotations

import argparse
import os
import sys

import uvicorn

# 默认监听地址（**不得改变**：前端 dev 代理与生产静态托管均默认指向它）
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

ENV_HOST = "KEFU_HOST"
ENV_PORT = "KEFU_PORT"


def _parse_port(value: object, origin: str) -> int:
    """把端口取值解析成合法端口，非法时以退出码 2 结束（中文提示）。

    Args:
        value: 待解析值（命令行字符串 / 环境变量字符串 / 默认整数）。
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


def resolve_address(argv: list[str] | None = None) -> tuple[str, int]:
    """按「命令行 > 环境变量 > 默认」解析监听地址。

    Args:
        argv: 命令行参数（不含程序名）；None = 取 ``sys.argv[1:]``。

    Returns:
        ``(host, port)``。
    """
    parser = argparse.ArgumentParser(
        description="kefu-system 客服系统（默认监听 127.0.0.1:8765）")
    parser.add_argument(
        "--host", default=None,
        help=f"监听地址（默认 {DEFAULT_HOST}，也可用环境变量 {ENV_HOST}）")
    parser.add_argument(
        "--port", default=None,
        help=f"监听端口（默认 {DEFAULT_PORT}，也可用环境变量 {ENV_PORT}）")
    args = parser.parse_args(argv)

    # 端口：命令行 > 环境变量 > 默认
    if args.port is not None and str(args.port).strip() != "":
        port = _parse_port(args.port, "命令行参数 --port")
    else:
        env_port = os.environ.get(ENV_PORT, "").strip()
        port = _parse_port(env_port, f"环境变量 {ENV_PORT}") \
            if env_port else DEFAULT_PORT

    # 地址：命令行 > 环境变量 > 默认（不做格式校验，交给 uvicorn）
    if args.host is not None and str(args.host).strip() != "":
        host = str(args.host).strip()
    else:
        host = os.environ.get(ENV_HOST, "").strip() or DEFAULT_HOST
    return host, port


def main() -> None:
    """启动 FastAPI 应用（单进程，兼容 GUI 自动化线程模型）。"""
    host, port = resolve_address()
    print(f"kefu-system 正在启动，监听地址：http://{host}:{port}")
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
