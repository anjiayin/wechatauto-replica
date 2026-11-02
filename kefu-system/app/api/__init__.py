# -*- coding: utf-8 -*-
"""API 路由汇总：main.py 一次性 include 全部子路由。"""
from __future__ import annotations

from app.api.router_opportunities import router as opportunities_router
from app.api.router_settings import router as settings_router
from app.api.router_system import router as system_router
from app.api.router_targets import router as targets_router
from app.api.router_types import router as types_router

ALL_ROUTERS = (
    system_router,
    targets_router,
    types_router,
    settings_router,
    opportunities_router,
)

__all__ = ["ALL_ROUTERS"]
