"""成本端点（AC-015-2/3；A3 §10 任务 8）。

| 方法 | 路径 | 响应 |
|---|---|---|
| GET | `/api/v1/costs/summary?dimension=` | `CostSummary`（三维：session/date/model，必填） |
| GET | `/api/v1/sessions/{session_id}/costs` | `SessionCost`（模型段 + 沙箱段） |

会话成本的两段来源：模型段（model_calls 聚合）由 cost_service 出；沙箱段（running/paused
分段估算）由 run_service 出（沙箱管理器的持有者）——本文件只做两段装配，无业务规则。
维度参数：契约 `QueryParamSpec(type=enum, required=True)`——FastAPI 以 Literal 校验，
缺失/值域外 → 422 `INVALID_ARGUMENT`（RequestValidationError 出口）。
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import ModelRegistryDep, RunServiceDep, get_db
from app.services.cost_service import CostService
from lcode_contracts.rest import CostSummary, SessionCost

router = APIRouter(tags=["costs"])

CostDimension = Literal["session", "date", "model"]
"""汇总维度（契约 `CostSummary.dimension` 三字面量；查询参数值域的唯一来源）。"""

DbDep = Annotated[AsyncSession, Depends(get_db)]


def get_cost_service(db: DbDep, registry: ModelRegistryDep) -> CostService:
    """成本 service 装配（注册表供单价估算）。"""
    return CostService(db, registry)


CostServiceDep = Annotated[CostService, Depends(get_cost_service)]


@router.get("/costs/summary", response_model=CostSummary, summary="成本三维汇总")
async def costs_summary(
    service: CostServiceDep,
    dimension: Annotated[CostDimension, Query(description="汇总维度（AC-015-2 三维，必填）")],
) -> CostSummary:
    """三维汇总：session（会话 id）/ date（YYYY-MM-DD，UTC）/ model（模型 id）。

    行序 = cost 降序、同额按 group_key 定序；`model_calls` 计行数（100% 落库口径，含失败行）。
    """
    return await service.summary(dimension)


@router.get(
    "/sessions/{session_id}/costs",
    response_model=SessionCost,
    summary="会话成本（模型+沙箱）",
)
async def session_costs(session_id: str, costs: CostServiceDep, runs: RunServiceDep) -> SessionCost:
    """会话成本：模型段（token + 费用）+ 沙箱段（running/paused 分段估算，AC-015-4）。

    会话不存在 → 404 `SESSION_NOT_FOUND`（两段装配任一校验先命中即返回）。
    """
    estimate = await runs.sandbox_estimate(session_id)
    return await costs.session_cost(session_id, sandbox=estimate)


__all__ = ["CostDimension", "CostServiceDep", "router"]
