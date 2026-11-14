# -*- coding: utf-8 -*-
"""Pydantic 请求模型（字段名与接口文档逐字一致）。

长度上限**必须自己写**：数据库是非严格模式（``sql_mode`` 只有
``NO_ENGINE_SUBSTITUTION``），超长字段会被**静默截断**，不会报错。
所以每个字符串字段都按对应列宽加 ``max_length``，由 Pydantic 拦在入口。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    """统一模型基类（忽略未知字段，便于客户端多传）。"""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=False)


# ---------------------------------------------------------------------------
# 客户端接口
# ---------------------------------------------------------------------------
class ActivateIn(_Base):
    """C1 激活请求。"""

    machine_code: str = Field(..., min_length=1, max_length=64,
                              description="本机机器码（32 位十六进制，可带横杠）")
    activation_code: str = Field("", max_length=64, description="激活码，可为空")
    app_version: str = Field("", max_length=32, description="客户端版本")
    os: str = Field("", max_length=64, description="操作系统描述")


class HeartbeatIn(_Base):
    """C2 心跳请求。"""

    machine_code: str = Field(..., min_length=1, max_length=64)
    license_id: str = Field("", max_length=32, description="本地许可证号，可空")
    app_version: str = Field("", max_length=32)


# ---------------------------------------------------------------------------
# 管理端鉴权
# ---------------------------------------------------------------------------
class LoginIn(_Base):
    """A1 登录请求。"""

    username: str = Field(..., min_length=1, max_length=32)
    password: str = Field(..., min_length=1, max_length=128)


class ChangePasswordIn(_Base):
    """A4 修改密码请求。"""

    old_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=1, max_length=128)


# ---------------------------------------------------------------------------
# 管理端业务
# ---------------------------------------------------------------------------
class CreateCodesIn(_Base):
    """B2 生成激活码。"""

    count: int = Field(1, ge=1, le=500, description="生成数量 1–500")
    days: int = Field(365, ge=1, le=3650, description="兑换后的有效天数")
    customer: str = Field("", max_length=64, description="客户名称（兼容 mock 入参名）")
    customer_phone: str = Field("", max_length=32, description="客户手机号")
    edition: str = Field("standard", max_length=32)
    note: str = Field("", max_length=255)
    batch: str = Field("", max_length=32, description="批次号，空则自动生成")


class ExtendIn(_Base):
    """B7 增减天数（可负）。

    范围校验放在服务层（``-3650 ~ 3650`` 且不能为 0），这样越界统一返回
    契约里的 ``invalid_days``，而不是 Pydantic 的 ``invalid_param``。
    """

    days: int = Field(365, description="正数增加、负数减少，不能为 0")
    force: bool = Field(False, description="到期日早于现在时需 true 才执行")


class RenewIn(_Base):
    """B6 延期（兼容 mock 的别名接口）。"""

    days: int = Field(365, ge=1, le=3650)


class SetExpiryIn(_Base):
    """B8 直接设到期日。"""

    expires_at: str = Field(..., min_length=4, max_length=40,
                            description="YYYY-MM-DD 或完整 ISO8601")
    force: bool = Field(False)


class RevokeIn(_Base):
    """B9 吊销。"""

    reason: str = Field("", max_length=255)
    force: bool = Field(False, description="该机器已有更新证时需 true")


class BindCustomerIn(_Base):
    """B18 给许可证补录/修改客户绑定（名称与手机号至少填一个）。"""

    customer: str = Field("", max_length=64, description="客户名称")
    phone: str = Field("", max_length=32, description="客户手机号")


class CreateCustomerIn(_Base):
    """B13 新增/更新客户档案。"""

    name: str = Field("", max_length=64)
    phone: str = Field(..., min_length=1, max_length=32)
    note: str = Field("", max_length=255)
