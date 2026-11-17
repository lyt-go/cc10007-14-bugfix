"""对比分析接口：维护对比组，覆盖提交对比审批、执行对比、归档对比等动作。

静态路径（`/export`、`/executions`）必须先于 `/{entry_id}` 注册，
否则 `/api/comparison/export` 会被整型路径参数吞掉并返回 422。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services import comparison_service

router = APIRouter(prefix="/api/comparison", tags=["对比分析"])

service = comparison_service

STATUSES = ["草稿", "对比审批中", "已执行", "已归档"]
ACTIONS = ["提交对比审批", "执行对比", "归档对比"]


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出对比分析清单：返回当前全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "comparison", "total": total, "items": items}


@router.get("/executions", response_model=dict)
def list_executions(
    code: str | None = Query(default=None, description="按对比编号回查执行回执"),
) -> dict[str, Any]:
    """对比组执行记录：每次执行都有独立编号与数据快照，供结项结论回查。"""
    comparison = service.get_entry_by_code(code) if code else None
    if code and comparison is None:
        raise HTTPException(status_code=404, detail=f"对比组 {code} 不存在或已归档")
    items = service.executions((comparison or {}).get("id"))
    return {"module": "comparison_execution", "total": len(items), "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按对比编号检索"),
    对比名称: str | None = Query(default=None, description="按对比名称检索"),
    实验组筛选: str | None = Query(default=None, description="按实验组筛选检索"),
    status: str | None = Query(default=None, description="草稿、对比审批中、已执行、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按编号 / 名称 / 实验组筛选 / 状态过滤对比分析列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword,
        status=status,
        page=page,
        size=size,
        name=对比名称,
        experiment_filter=实验组筛选,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条对比组明细（含最新执行回执）；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"对比组 {entry_id} 不存在或已归档")
    payload = dict(entry)
    payload["最新执行"] = service.latest_execution(entry)
    return payload


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条对比组，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="对比组已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条对比组执行提交对比审批、执行对比、归档对比；不允许的动作会被拦下并说明原因。

    `values` 里可带观测数据（对照组/实验组样本量与转化数），执行时写入快照。
    """
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
