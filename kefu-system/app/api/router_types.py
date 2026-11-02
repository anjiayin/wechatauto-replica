# -*- coding: utf-8 -*-
"""商机类型 CRUD 路由。

路由（设计文档 3.2）：
- GET    /api/opportunity-types
- POST   /api/opportunity-types         {name, description, enabled?}
- PATCH  /api/opportunity-types/{id}    {name?, description?, enabled?}
- DELETE /api/opportunity-types/{id}    → 204
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response

from app.api.deps import get_db
from app.db import Database
from app.schemas import (
    OpportunityTypeCreate,
    OpportunityTypeOut,
    OpportunityTypeUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/opportunity-types", tags=["opportunity-types"])


def _to_out(row: dict) -> OpportunityTypeOut:
    return OpportunityTypeOut(
        id=row["id"], name=row["name"], description=row["description"],
        enabled=row["enabled"], updated_at=row.get("updated_at", ""),
    )


@router.get("", response_model=list[OpportunityTypeOut])
def list_types(db: Database = Depends(get_db)) -> list[OpportunityTypeOut]:
    """商机类型列表。"""
    return [_to_out(t) for t in db.list_types()]


@router.post("", response_model=OpportunityTypeOut, status_code=201)
def create_type(body: OpportunityTypeCreate,
                db: Database = Depends(get_db)) -> OpportunityTypeOut:
    """新增商机类型（自然语言描述）。"""
    name = body.name.strip()
    description = body.description.strip()
    if not name or not description:
        raise HTTPException(status_code=422, detail="类型名称与描述均不能为空")
    row = db.insert_type(name, description, enabled=1 if body.enabled else 0)
    logger.info("新增商机类型：%s", name)
    return _to_out(row)


@router.patch("/{type_id}", response_model=OpportunityTypeOut)
def update_type(type_id: int, body: OpportunityTypeUpdate,
                db: Database = Depends(get_db)) -> OpportunityTypeOut:
    """修改/启停商机类型（后续消息立即按新描述生效）。"""
    if not db.get_type(type_id):
        raise HTTPException(status_code=404, detail="商机类型不存在")
    fields: dict = {}
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="类型名称不能为空")
        fields["name"] = name
    if body.description is not None:
        desc = body.description.strip()
        if not desc:
            raise HTTPException(status_code=422, detail="类型描述不能为空")
        fields["description"] = desc
    if body.enabled is not None:
        fields["enabled"] = 1 if body.enabled else 0
    row = db.update_type(type_id, fields)
    logger.info("更新商机类型 #%d：%s", type_id, list(fields))
    return _to_out(row)  # type: ignore[arg-type]


@router.delete("/{type_id}", status_code=204)
def delete_type(type_id: int, db: Database = Depends(get_db)) -> Response:
    """删除商机类型（关联商机的 type_id 置空，历史快照保留）。"""
    if not db.get_type(type_id):
        raise HTTPException(status_code=404, detail="商机类型不存在")
    db.delete_type(type_id)
    logger.info("删除商机类型 #%d", type_id)
    return Response(status_code=204)
