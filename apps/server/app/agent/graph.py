"""外层 LangGraph 编排图（A3 §5.1）：M2 = entry → router → execute。

结构裁定（安装版 langgraph 1.2.16 实测，见模块 docstring 与 state.py）：

- **execute 节点 = 编译后的内层 deepagents 图本身**（`add_node` 传 CompiledStateGraph
  即成为子图节点）：内层事件经 `astream(subgraphs=True)` 以
  `(namespace, mode, payload)` 三元组流出——translator 的 U2 AgentRef 推导依赖该形态，
  本车道探针已固化；
- 内层 agent 由 RunExecutor 每次 run 装配后经 `inner_agent` 参数挂入（编译图不能进
  checkpointer state，A3 §5.1 的"entry 装配入 state"不可行——见 state.py 裁定）；
- checkpointer 传给外层 `compile()`：InMemorySaver（integration）/ AsyncPostgresSaver
  （e2e/部署）均可；子图 checkpoint 挂在父线程的 checkpoint_ns 下，M3 的审批
  interrupt 恢复依赖它（REQ-004：仅运行时断点恢复，禁止反序列化做展示/审计）。

plan_mode（M3）/ fan_out+reduce（M4）不实现：router 的判定面已在 `state.Route`
预留，对应节点届时以增量接入（M2 派单边界）。
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer

from app.agent.nodes import entry_node, router_node
from app.agent.state import OuterState

EXECUTE_NODE = "execute"
"""execute 节点名（内层 deepagents 子图的挂载名；translator 命名空间首段）。"""


def build_graph(
    checkpointer: Checkpointer | None,
    inner_agent: CompiledStateGraph,
) -> CompiledStateGraph:
    """构建外层图并挂载内层 agent（每次 run 重建内层后调用——AC-014-2）。"""
    builder = StateGraph(OuterState)
    builder.add_node("entry", entry_node)
    builder.add_node("router", router_node)
    builder.add_node(EXECUTE_NODE, inner_agent)
    builder.add_edge(START, "entry")
    builder.add_edge("entry", "router")
    builder.add_edge("router", EXECUTE_NODE)
    builder.add_edge(EXECUTE_NODE, END)
    return builder.compile(checkpointer=checkpointer)


__all__ = ["EXECUTE_NODE", "build_graph"]
