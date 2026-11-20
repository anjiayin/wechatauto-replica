# -*- coding: utf-8 -*-
"""AI 账单单价读取与 token→金额换算（预算预检与账单页共享的唯一真源）。

从 ``router_system._billing_settings`` / ``_usage_cost`` 原样抽出，公式与
``GET /api/system/billing`` 完全一致，避免预算预检与实际账单换算漂移。
本模块无状态、不依赖 API 层，service 层可直接调用。
"""
from __future__ import annotations

from app.db import Database


def get_billing_settings(db: Database) -> dict:
    """读取账单单价配置：input_price/output_price/unit（per_k|per_m）。

    未配置（或配置非法）时用「元/百万 tokens」；用户已保存的单位必须尊重。
    """
    unit = db.get_setting("billing.unit", "per_m")
    if unit not in ("per_k", "per_m"):
        unit = "per_m"
    return {
        "unit": unit,
        "input_price": db.get_float("billing.input_price", 0.0),
        "output_price": db.get_float("billing.output_price", 0.0),
    }


def estimate_cost(prompt_tokens: int, completion_tokens: int,
                  billing: dict) -> float:
    """按单价与单位换算 token 用量的费用（元）。

    per_k：元/千 tokens；per_m：元/百万 tokens。未配置单价时为 0。
    """
    divisor = 1000.0 if billing["unit"] == "per_k" else 1_000_000.0
    pt = int(prompt_tokens or 0)
    ct = int(completion_tokens or 0)
    return (pt / divisor * billing["input_price"]
            + ct / divisor * billing["output_price"])
