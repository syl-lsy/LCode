"""diff 审查端点（契约 diff-reviews 三端点；REQ-007；M3 Phase 4）。

| 方法 | 路径 | 请求 → 响应 |
|---|---|---|
| GET  | `/api/v1/diff-reviews/{review_id}` | → `DiffReviewDetail`（逐文件摘要） |
| GET  | `.../{review_id}/files/{file_path:path}` | → `DiffFileDetail`（完整 patch） |
| POST | `/api/v1/diff-reviews/{review_id}/verdict` | `DiffVerdictRequest` → `DiffReviewDetail` |

`file_path` **必须用 `:path` 路由转换器**：路径参数本身含 `/`（契约端点 summary 已钉死），
`/files/src/main.py` 这类请求才能落到本路由。

服务端编排（`services/diff_service.py`）：

- GET 详情/文件：只读投影（files jsonb 的内部超集 → 契约摘要；binary 与 >1MB 截断的
  `patch=null`，正文引用走 `blob_ref`）；
- POST 裁决：accepted → 沙箱内 commit+tag 推进基线并写 `git_baselines`（AC-007-4）；
  rejected → 保存意见、`git reset` 保持工作区可编辑，并在有非空 feedback 时经消息通道
  自动起返工 run（会话已有活动 run 时只记录裁决与系统提示，不建 run）；
- 两条写路径都在 emit 前显式 commit（事件永不引用未提交事实）。

错误码（契约端点 errors，逐条对齐）：未知/非法审查 id → 404 `DIFF_REVIEW_NOT_FOUND`；
重复裁决 → 409 `DIFF_ALREADY_RESOLVED`；文件不在本轮 diff → 404 `NOT_FOUND`；载荷非法
→ 422 `INVALID_ARGUMENT`（契约 DTO 闸门）。一律走全局异常处理器出口，不手拼 JSONResponse。
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import EventPipelineDep, RunServiceDep, SandboxManagerDep, get_db
from app.services.diff_service import DiffService
from app.services.message_service import MessageService
from lcode_contracts.rest import DiffFileDetail, DiffReviewDetail, DiffVerdictRequest

router = APIRouter(prefix="/diff-reviews", tags=["diff-reviews"])

DbDep = Annotated[AsyncSession, Depends(get_db)]
"""请求级事务会话（裁决在 emit 前显式 commit，与 approvals 决策同口径）。"""


def get_diff_service(
    db: DbDep,
    pipeline: EventPipelineDep,
    runs: RunServiceDep,
    manager: SandboxManagerDep,
) -> DiffService:
    """装配 diff service：沙箱管理器（git 操作在沙箱内）+ 返工启动器（消息通道）。

    返工复用既有「发送消息」通道（`MessageService.post_message`，run 启动器接
    `run_service.start_run`）——不新造事件、不新造 run 入口（m3-plan Phase 4）。
    """
    messages = MessageService(db, pipeline=pipeline, run_starter=runs.start_run)
    return DiffService(
        db,
        pipeline=pipeline,
        manager=manager,
        rework_starter=messages.post_message,
    )


DiffServiceDep = Annotated[DiffService, Depends(get_diff_service)]


@router.get("/{review_id}", response_model=DiffReviewDetail, summary="diff 审查详情（逐文件摘要）")
async def get_diff_review(review_id: str, service: DiffServiceDep) -> DiffReviewDetail:
    """审查详情（含基线引用/轮次/裁决状态/totals）；404 `DIFF_REVIEW_NOT_FOUND`。"""
    return await service.get_review(review_id)


@router.get(
    "/{review_id}/files/{file_path:path}",
    response_model=DiffFileDetail,
    summary="单文件完整 patch（审查面板加载；file_path 含 `/`）",
)
async def get_diff_file(review_id: str, file_path: str, service: DiffServiceDep) -> DiffFileDetail:
    """单文件 patch；binary/超限截断返回 `patch=null` + `blob_ref`。

    文件不在该轮 diff → 404 `NOT_FOUND`（契约兜底码）；审查不存在 → 404
    `DIFF_REVIEW_NOT_FOUND`。
    """
    return await service.get_file(review_id, file_path)


@router.post(
    "/{review_id}/verdict",
    response_model=DiffReviewDetail,
    summary="diff 裁决（accepted 后创建基线提交）",
)
async def post_diff_verdict(
    review_id: str, payload: DiffVerdictRequest, service: DiffServiceDep
) -> DiffReviewDetail:
    """accepted 推进基线（AC-007-4）；rejected 保存意见并按需自动返工（AC-007-3）。"""
    return await service.verdict(review_id, payload)


__all__ = ["DiffServiceDep", "router"]
