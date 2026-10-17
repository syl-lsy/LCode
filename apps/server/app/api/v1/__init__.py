"""v1 路由聚合点：一资源一文件（A3 §1.1），全部挂 `/api/v1` 前缀（ADR-004 偏差 8）。

M1 骨架只提供挂载结构（无端点）——契约 `rest.py` 的 19 个端点按资源归属拆成
9 个路由文件，M2 逐个 `include_router` 到本聚合点（A3 §10 任务 3/7/8）：

| 文件 | 端点 | M2 任务 |
|---|---|---|
| `sessions.py` | POST/GET 列表/GET 详情/PATCH/DELETE `/sessions` | 7 |
| `messages.py` | POST/GET `/sessions/{session_id}/messages` | 7 |
| `events.py` | GET `/sessions/{session_id}/events`（SSE，唯一流式端点） | 7 |
| `runs.py` | POST `/runs/{run_id}/stop`（幂等停止） | 6 |
| `approvals.py` | GET `/approvals`、GET `/approvals/{id}`、POST `/approvals/{id}/decide` | M3 |
| `models.py` | GET `/models`（模型列表，AC-014-1） | 3 |
| `costs.py` | GET `/costs/summary`、GET `/sessions/{session_id}/costs` | 8 |
| `sandbox.py` | GET `/sessions/{session_id}/sandbox`（状态与费用估算） | 4 |
| `diff_reviews.py` | GET `/diff-reviews/{id}`、文件与裁决端点 | M3 |

`diff_reviews` 的行内细节：GET `.../files/{file_path:path}`、POST `.../verdict`；
文件路由必须用 `{file_path:path}` 转换器——`file_path` 本身含 `/`。

约定：路由函数只做路由 + 参数校验 + 依赖注入，业务逻辑下沉 `app/services/`；
SSE 端点必须自设 `request.state.is_stream = True`（跳过 `/api/` 成功信封）。
"""

from fastapi import APIRouter

api_router = APIRouter(prefix="/api/v1")

# M2 起逐个挂载（示例，勿提前实现）：
# from app.api.v1 import sessions
# api_router.include_router(sessions.router)

__all__ = ["api_router"]
