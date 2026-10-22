"""审批端点（契约 approvals 三端点；REQ-006；M3 Phase 1）。

| 方法 | 路径 | 请求 → 响应 |
|---|---|---|
| GET  | `/api/v1/approvals` | `?status=` / `?session_id=` → `ApprovalPage`（AC-006-5） |
| GET  | `/api/v1/approvals/{approval_id}` | → `ApprovalItem` |
| POST | `/api/v1/approvals/{approval_id}/decide` | 决策请求 → `ApprovalDecisionAck`（+resume） |

`decide` 的服务端编排（`approval_service.decide`）：行落库 → commit → 发
`approval_resolved` → 同 run 全部 pending 已解决时经注入的 `resume_starter`
（= `run_service.resume_run`）调度续跑；本文件只做路由/参数校验/装配。

错误码（契约端点 errors）：未知审批 → 404 `APPROVAL_NOT_FOUND`；重复决策 →
409 `APPROVAL_ALREADY_RESOLVED`；edit 缺 edited_args / respond 缺 feedback →
422 `INVALID_ARGUMENT`（契约 DTO 与 service 双层守门）。
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import EventPipelineDep, RunServiceDep, get_db
from app.services.approval_service import ApprovalService
from lcode_contracts.rest import (
    ApprovalDecisionAck,
    ApprovalDecisionRequest,
    ApprovalItem,
    ApprovalPage,
)

router = APIRouter(prefix="/approvals", tags=["approvals"])

DbDep = Annotated[AsyncSession, Depends(get_db)]

ApprovalStatusQuery = Literal[
    "pending", "approved", "rejected", "edited", "responded", "auto_approved"
]
"""`?status=` 值域（契约 QueryParamSpec enum 同源；值域外 → 422 INVALID_ARGUMENT）。"""


def get_approval_service(
    db: DbDep,
    pipeline: EventPipelineDep,
    runs: RunServiceDep,
) -> ApprovalService:
    """审批 service 装配：resume_starter 直接接 run_service.resume_run（服务级编排）。

    `RunServiceDep` 是 api ↔ services 的合法桥（同 messages/runs 车道）；审批决定齐备后
    由 service 触发 resume，路由不承担编排逻辑。
    """
    return ApprovalService(
        db,
        pipeline=pipeline,
        resume_starter=runs.resume_run,
    )


ApprovalServiceDep = Annotated[ApprovalService, Depends(get_approval_service)]


@router.get("", response_model=ApprovalPage, summary="审批列表（status=pending 恢复挂起卡片）")
async def list_approvals(
    service: ApprovalServiceDep,
    status: Annotated[
        ApprovalStatusQuery | None,
        Query(description="按状态过滤（AC-006-5 刷新恢复用 status=pending）"),
    ] = None,
    session_id: Annotated[str | None, Query(description="按会话过滤")] = None,
) -> ApprovalPage:
    """审批列表：最新在前；`status=pending&session_id=` 是刷新恢复的入口。"""
    return await service.list_approvals(status=status, session_id=session_id)


@router.get("/{approval_id}", response_model=ApprovalItem, summary="审批详情")
async def get_approval(approval_id: str, service: ApprovalServiceDep) -> ApprovalItem:
    """审批详情（404 `APPROVAL_NOT_FOUND`）。"""
    return await service.get_approval(approval_id)


@router.post(
    "/{approval_id}/decide",
    response_model=ApprovalDecisionAck,
    summary="审批决策（四决策 + resume）",
)
async def decide_approval(
    approval_id: str,
    payload: ApprovalDecisionRequest,
    service: ApprovalServiceDep,
) -> ApprovalDecisionAck:
    """四决策（approve/edit/reject/respond）→ 落行 → 广播 → （全部齐备时）resume。"""
    approval, run_id = await service.decide(approval_id, payload)
    return ApprovalDecisionAck(approval=approval, run_id=run_id)


__all__ = ["ApprovalServiceDep", "router"]
