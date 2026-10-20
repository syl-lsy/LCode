"""router 节点：宏观路径判定（A3 §5.1；REQ-009/014-4）。

判定策略：**规则优先，能规则判的不调模型**（经济档 roles.router 的模型判定是
规则未命中时的 M3+ 手段）。M2 判定面：

- `workflow` 字段非空 → 内置工作流路径（fan_out，M4 预留：本车道直接拒绝——
  该路径无对应节点，宁可显式报错不静默走错分支）；
- 其余 → 常规执行（execute）。计划模式（"先出计划"类指令的 plan_mode）与
  内置工作流匹配在 M3/M4 落地时在此扩展 `route_for()` 并接入对应节点。
"""

from __future__ import annotations

from typing import Any

from app.agent.state import OuterState, Route

DEFAULT_ROUTE: Route = "execute"
"""M2 常规执行默认路径（冻结设计：外层图 M2 只做 entry→router→execute）。"""


def route_for(state: OuterState) -> Route:
    """规则前置判定（M2：workflow 占位拒绝 + 默认常规执行）。"""
    if state.get("workflow"):
        # M4 接线点：届时返回 "fan_out" 并接入 Send×N 节点
        raise NotImplementedError("内置工作流路径是 M4 范围：workflow 分片不该出现在 M2 run")
    return DEFAULT_ROUTE


def router_node(state: OuterState) -> dict[str, Any]:
    return {"route": route_for(state)}


__all__ = ["DEFAULT_ROUTE", "route_for", "router_node"]
