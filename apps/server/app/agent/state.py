"""外层编排图状态（A3 §5.1）：TypedDict 快照 + M3/M4 预留字段。

设计裁定（与本车道探针一致，安装版 langgraph 1.2.16 实测）：

- `messages` 走 `add_messages` reducer：execute（内层 deepagents 子图）产出的消息
  以增量并入外层状态，多轮上下文由 checkpointer（thread_id = user:session）续住；
- **内层 agent 实例不进 state**：编译图对象过不了 checkpointer 序列化，且 A3 §5.1
  的"entry 装配内层 agent 入 state"在该约束下不可行——装配改由 RunExecutor 每次
  run 重建后以编译图形态作为 execute 节点挂进外层图（AC-014-2 "下一条消息起生效"
  天然成立）。`graph.build_graph(checkpointer, inner_agent=...)` 是唯一挂载点；
- `workflow` / `fanout_results` 是 M3（plan_mode）/ M4（fan_out Send×N / reduce）的
  预留字段：本车道只保留字段与注释，不实现对应节点（M2 图 = entry→router→execute）。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

Route = Literal["execute", "plan_mode", "fan_out"]
"""router 的判定面：M2 只产 execute；plan_mode（M3）/ fan_out（M4）占位。"""


class SessionCfg(TypedDict, total=False):
    """会话快照（sessions 行投影；A3 §5.1 session_cfg 字段）。

    全部可 JSON 序列化（checkpoint 友好）。`approval_mode` / `sandbox_provider`
    由 run_service（服务车道）读会话行后随 run 启动注入；M2 装配只消费
    model / reasoning_effort 两旋钮（REQ-014）。
    """

    model: str | None
    reasoning_effort: str | None
    approval_mode: str
    sandbox_provider: str


class OuterState(TypedDict, total=False):
    """外层图状态。"""

    messages: Annotated[list[AnyMessage], add_messages]
    """对话锚点（唯一带 reducer 的字段；execute 子图输出经此并入）。"""

    session_id: str
    run_id: str
    user_id: str

    session_cfg: SessionCfg
    """会话快照（模型/档位/审批模式/provider）——run 启动时由 RunExecutor 注入。"""

    route: Route
    """router 判定结果（M2 恒 execute）。"""

    workflow: dict[str, Any] | None
    """M4 预留：内置工作流定义与分片清单（fan_out Send×N 的输入）。本车道不写。"""

    fanout_results: list[dict[str, Any]]
    """M4 预留：reduce 汇总的 N 份结果。本车道不写。"""


__all__ = ["OuterState", "Route", "SessionCfg"]
