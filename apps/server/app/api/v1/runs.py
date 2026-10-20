"""run 端点（AC-001-2；A3 §10 任务 6/7 的编排出口）。

| 方法 | 路径 | 响应 |
|---|---|---|
| POST | `/api/v1/runs/{run_id}/stop` | `StopRunAck`（幂等：status 恒 `stopped`） |

错误路径（契约端点 errors）：未知 run → 404 `RUN_NOT_FOUND`（含非法 UUID，同口径）；
对非运行中的 run 下 stop → 409 `RUN_NOT_ACTIVE`；已 stopped 的 run 重复 stop → 200 +
`stopped`（契约 `StopRunAck` 注释的幂等语义——「停止」是收敛操作，重复表达同一意图不报错）。

stop 的**诚实收敛**在 run_service：返回 ack 时 `message_finished(cancelled)` +
`run_status(stopped)` 事件与 run 行终态都已落库（不是「已请求停止」的中间态）。
service 装配（含 checkpointer/model_factory 接缝）统一在 `app.dependencies.get_run_service`。
"""

from fastapi import APIRouter

from app.dependencies import RunServiceDep
from app.services.run_service import RunService
from lcode_contracts.rest import StopRunAck

router = APIRouter(prefix="/runs", tags=["runs"])


@router.post("/{run_id}/stop", response_model=StopRunAck, summary="停止运行（幂等）")
async def stop_run(run_id: str, service: RunServiceDep) -> StopRunAck:
    """停止运行：取消执行任务并等待收口（事件 + run 行终态落库）后返回。"""
    return await service.stop_run(run_id)


__all__ = ["router", "RunService"]
