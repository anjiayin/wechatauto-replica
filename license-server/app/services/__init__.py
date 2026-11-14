# -*- coding: utf-8 -*-
"""服务层：客户端三接口的业务编排（激活四分支 / 心跳四分支 / 查询）。"""

from app.services import admin_service, license_service  # noqa: F401

__all__ = ["admin_service", "license_service"]
