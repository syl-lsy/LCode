"""entry 节点：发 `run_status(started)`（A3 §5.1 表；AC-001 的运行状态机起点）。

职责边界（与 A3 §5.1 表的两处偏差，均为安装版约束下的裁定，见 state.py/graph.py）：

- **不装配内层 agent**：编译图过不了 checkpointer 序列化，装配移至 RunExecutor
  每次 run 重建后经 `build_graph(inner_agent=...)` 挂载（AC-014-2 语义不变）；
- **不读会话行**：`agent/` 不得 import `models/`（A3 §1.2 依赖方向），会话快照
  （model/effort/approval_mode/provider）由 run_service（服务车道）在 run 启动时
  读行注入 state/config——本节点只消费快照并宣告 started。

事件出口纪律（A3 §6.2 / AC-008-2）：经 LangGraph 框架设施 `get_stream_writer()`
发 custom 流，载荷用 translator 的 custom 通道协议
`{"type": "run_status", "subject": "run", "phase": "started"}`——不 import 业务事件包，
不透传框架原始对象。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langgraph.config import get_stream_writer

from app.agent.state import OuterState

RUN_STARTED_PAYLOAD: dict[str, str] = {
    "type": "run_status",
    "subject": "run",
    "phase": "started",
}


def entry_node(state: OuterState) -> dict[str, Any]:
    """宣告 run 开始；状态直通（M2 entry 无状态变更）。"""
    writer = get_stream_writer()
    writer(dict(RUN_STARTED_PAYLOAD))
    # 会话快照存在性校验：缺快照说明 run_service 车道接线不完整，fail loudly
    if not isinstance(state.get("session_cfg"), Mapping):
        raise ValueError("entry：state.session_cfg 缺失——run_service 必须注入会话快照")
    return {}


__all__ = ["RUN_STARTED_PAYLOAD", "entry_node"]
