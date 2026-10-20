"""外层图节点：entry / router（M2）；plan_mode / fan_out / reduce 为 M3/M4 预留。"""

from app.agent.nodes.entry import entry_node
from app.agent.nodes.router import route_for, router_node

__all__ = ["entry_node", "route_for", "router_node"]
