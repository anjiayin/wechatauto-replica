# -*- coding: utf-8 -*-
"""kefu-system 启动入口。

用法（在 kefu-system 目录下）::

    python run.py

默认监听 127.0.0.1:8765，前端 dev 代理与生产静态托管均指向该端口。
"""
from __future__ import annotations

import uvicorn


def main() -> None:
    """启动 FastAPI 应用（单进程，兼容 GUI 自动化线程模型）。"""
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8765,
        log_level="info",
    )


if __name__ == "__main__":
    main()
