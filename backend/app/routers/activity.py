"""活动评估接口：维护活动方案，覆盖提交方案评审、启动评估、结项评估、取消方案等动作。

契约要点：
- 列表/详情/导出保持 `{items, total, page, size}` 与 `{ok, message}` 的统一形状。
- 静态路径（`/export`、`/assessment`、`/results`）必须注册在 `/{entry_id}` 之前，
  否则会被整型路径参数吞掉并返回 422——这是页面数据「丢失/错位」的根因之一。
- 结项评估额外返回 `assessment`，并支持多选整组提交与逐条核对结果。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, BatchResult, EntryPayload, PageResult
from app.services.activity import _BatchRejected
from app.services import activity_service

router = APIRouter(prefix="/api/activity", tags=["活动评估"])

service = activity_service

STATUSES = ["草稿", "方案评审中", "评估中", "已结项", "已取消"]
ACTIONS = ["提交方案评审", "启动评估", "结项评估", "取消方案"]


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出活动评估清单：返回当前全量数据（含结项字段）。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "activity", "total": total, "items": items}


@router.get("/assessment", response_model=dict)
def list_assessments(
    code: str | None = Query(default=None, description="按方案编号回查结项明细"),
    include_audit: bool = True,
) -> dict[str, Any]:
    """活动结项评估明细数据集：按方案编号回查，默认带审计明细。"""
    activity = service.get_entry_by_code(code) if code else None
    if code and activity is None:
        raise HTTPException(status_code=404, detail=f"活动方案 {code} 不存在或已归档")
    items = service.assessments(activity, include_audit=include_audit)
    return {"module": "activity_assessment", "total": len(items), "items": items}


@router.get("/results", response_model=dict)
def result_lookup(code: str = Query(description="方案编号，例如 ACTI-0001")) -> dict[str, Any]:
    """结果回查：活动状态、可引用执行回执、结项明细、被拒绝的对比引用与审计。"""
    activity = service.get_entry_by_code(code)
    if activity is None:
        raise HTTPException(status_code=404, detail=f"活动方案 {code} 不存在或已归档")
    return service.result_lookup(activity)


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按方案编号检索"),
    活动名称: str | None = Query(default=None, description="按活动名称检索"),
    活动类型: str | None = Query(default=None, description="按活动类型检索"),
    status: str | None = Query(default=None, description="草稿、方案评审中、评估中、已结项、已取消"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按编号 / 名称 / 类型 / 状态过滤活动评估列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword,
        status=status,
        page=page,
        size=size,
        name=活动名称,
        activity_type=活动类型,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条活动方案明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"活动方案 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条活动方案，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="活动方案已登记", entry=entry)


@router.post("/batch", response_model=BatchResult)
def run_batch(payload: EntryPayload) -> BatchResult:
    """多选整组提交：`ids` + `action`，逐条核对后整组提交；任一条不通过则整组不落库。"""
    raw_ids = payload.values.get("ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return BatchResult(ok=False, message="请至少选择一条活动方案", checks=[])
    try:
        ids = [int(item) for item in raw_ids]
    except (TypeError, ValueError):
        return BatchResult(ok=False, message="所选记录编号必须是整数", checks=[])
    action = str(payload.values.get("action") or "").strip()
    try:
        ok, message, checks, created = service.run_batch(ids, action, payload.values)
    except _BatchRejected as rejected:
        return BatchResult(
            ok=False,
            message="整组提交未通过核对，未产生任何结项记录",
            checks=rejected.checks,
        )
    return BatchResult(
        ok=ok,
        message=message,
        checks=checks,
        assessments=created,
    )


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条活动方案执行提交方案评审、启动评估、结项评估、取消方案。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message, assessment = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry, assessment=assessment)
