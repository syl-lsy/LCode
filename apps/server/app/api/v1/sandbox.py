"""沙箱状态端点（AC-002-6 / AC-015-4；A3 §10 任务 4 的查询面）。

| 方法 | 路径 | 响应 |
|---|---|---|
| GET | `/api/v1/sessions/{session_id}/sandbox` | `SandboxStatus`（provider/相位/费用估算） |

`phase` 是**查询侧稳态**（resumed/recreated 迁移动作折叠为 running，契约 SandboxStatus
注释口径）；折叠发生在 run_service（沙箱管理器持有者），本文件零业务规则、零沙箱引用
（A3 §1.2：`api/` 不碰 sandbox，经 service/dependencies）。
"""

from fastapi import APIRouter

from app.dependencies import RunServiceDep
from lcode_contracts.rest import SandboxStatus

router = APIRouter(tags=["sandbox"])


@router.get(
    "/sessions/{session_id}/sandbox",
    response_model=SandboxStatus,
    summary="沙箱状态与费用估算",
)
async def sandbox_status(session_id: str, service: RunServiceDep) -> SandboxStatus:
    """沙箱状态投影：provider（会话固化值）/ 稳态相位 / running·paused 起始时间 / 费用估算。

    会话不存在 → 404 `SESSION_NOT_FOUND`；`phase="none"` 表示尚无沙箱实例。
    """
    return await service.sandbox_status(session_id)


__all__ = ["router"]
