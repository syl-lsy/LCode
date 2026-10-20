"""v1 路由聚合点：一资源一文件（A3 §1.1），全部挂 `/api/v1` 前缀（ADR-004 偏差 8）。

契约 `rest.py` 的 19 个端点按资源归属拆成 9 个路由文件，M2 已挂 7 个（A3 §10 任务 3/7/8）：

| 文件 | 端点 | 状态 |
|---|---|---|
| `sessions.py` | POST / GET 列表 / GET 详情 / PATCH / DELETE `/sessions`（DELETE 联动 kill） | M2 |
| `messages.py` | POST / GET `/sessions/{session_id}/messages` | M2 |
| `events.py` | GET `/sessions/{session_id}/events`（SSE，唯一流式端点） | M2 |
| `runs.py` | POST `/runs/{run_id}/stop`（幂等停止） | M2 |
| `models.py` | GET `/models`（模型列表，AC-014-1） | M2 |
| `costs.py` | GET `/costs/summary`、GET `/sessions/{session_id}/costs` | M2 |
| `sandbox.py` | GET `/sessions/{session_id}/sandbox`（状态与费用估算） | M2 |
| `approvals.py` | GET `/approvals`、GET `/approvals/{id}`、POST `/approvals/{id}/decide` | M3 |
| `diff_reviews.py` | GET `/diff-reviews/{id}`、文件与裁决端点 | M3 |

`diff_reviews` 的行内细节：GET `.../files/{file_path:path}`、POST `.../verdict`；
文件路由必须用 `{file_path:path}` 转换器——`file_path` 本身含 `/`。

约定：路由函数只做路由 + 参数校验 + 依赖注入，业务逻辑下沉 `app/services/`；
SSE 端点自设 `request.state.is_stream = True`（跳过 `/api/` 成功信封，已落在 events.py）。
"""

from fastapi import APIRouter

from app.api.v1 import costs, events, messages, models, runs, sandbox, sessions

api_router = APIRouter(prefix="/api/v1")

# M2 挂载（A3 §10 任务 3/7/8；M3 的 approvals/diff_reviews 届时增量接入）：
api_router.include_router(models.router)  # 任务 3：GET /models（AC-014-1）
api_router.include_router(sessions.router)  # 任务 7：/sessions 五端点（含 DELETE 联动 kill）
api_router.include_router(messages.router)  # 任务 7：POST/GET /sessions/{id}/messages
api_router.include_router(events.router)  # 任务 7：GET /sessions/{id}/events（SSE）
api_router.include_router(runs.router)  # 任务 6/7：POST /runs/{id}/stop（幂等）
api_router.include_router(costs.router)  # 任务 8：GET /costs/summary + /sessions/{id}/costs
api_router.include_router(sandbox.router)  # 任务 4/8：GET /sessions/{id}/sandbox

__all__ = ["api_router"]
