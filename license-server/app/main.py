# -*- coding: utf-8 -*-
"""应用工厂：注册异常处理器 / 路由 / 静态管理台。

路由注册顺序很重要：**业务路由必须在 ``StaticFiles`` 挂载之前注册**
（FastAPI 按注册顺序匹配，``/admin`` 挂了静态目录后，后注册的
``/admin/api/*`` 会被静态目录抢走）。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__, boot, db, signing
from app.api import admin_api, admin_auth, client_api
from app.config import get_config
from app.errors import register_exception_handlers

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """日志格式（中文时间 + 级别 + 模块 + 消息）。"""
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动：建表 → 首启建号 → 清会话 → 打印自检；关闭：释放连接池。"""
    boot.run_startup(verbose=True)
    logger.info("授权服务后台已启动（版本 %s）", __version__)
    try:
        yield
    finally:
        db.close_pool()
        logger.info("授权服务后台已停止，连接池已释放")


def create_app() -> FastAPI:
    """创建 FastAPI 应用。"""
    _configure_logging()
    cfg = get_config()

    # 启动前先把私钥读进内存：失败直接 SystemExit(2)，不静默启动
    signing.get_private_key()

    app = FastAPI(
        title="授权服务后台（license-server）",
        version=__version__,
        description="电脑授权服务：激活 / 心跳 / 许可证管理与审计",
        lifespan=lifespan,
        docs_url="/docs" if cfg.enable_docs else None,
        redoc_url="/redoc" if cfg.enable_docs else None,
        openapi_url="/openapi.json" if cfg.enable_docs else None,
    )
    register_exception_handlers(app)

    # 业务路由（必须在静态挂载之前）
    app.include_router(client_api.router)
    app.include_router(admin_auth.router)
    app.include_router(admin_api.router)

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        """根路径跳管理台。"""
        return RedirectResponse(url="/admin/", status_code=302)

    static_dir = cfg.static_dir
    if static_dir.exists():
        app.mount("/admin", StaticFiles(directory=str(static_dir), html=True),
                  name="admin")
        logger.info("管理台静态目录：%s", static_dir)
    else:                                       # 极端情况：目录被删也不崩
        logger.warning("管理台静态目录不存在：%s（/admin/ 将返回 404）",
                       static_dir)
    return app


#: uvicorn / gunicorn 可直接引用 ``app.main:app``
app = create_app()
