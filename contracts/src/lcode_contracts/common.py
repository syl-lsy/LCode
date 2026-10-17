"""契约公共类型：严格基模型、agent 归属、用量、todo 条目、档位。

所有 wire 模型继承 ContractModel：extra="forbid" —— 契约冻结后，未知字段
（拼写错误/私货字段）必须被拒收，这是"冻结"二字的机器表达。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentRef(ContractModel):
    """事件归属的子智能体标识；envelope.agent 为 null 表示主智能体。

    - agent_id：deepagents task() 派生的子线程标识
    - role：general-purpose / explorer / coder / reviewer 或 P1 自定义
    - spawned_by_tool_call_id：派生该子智能体的 task() 工具调用 id —— 前端用它把
      主流中的 task() 工具卡片与子智能体进度面板（message_delta 兄弟流）关联
    """

    agent_id: str
    role: str | None = None
    parent_agent_id: str | None = None
    spawned_by_tool_call_id: str | None = None


class Usage(ContractModel):
    """token 用量快照。口径（PRD 硬约束 9）：思考系模型的推理 token 计入 output。"""

    input_tokens: int = 0
    output_tokens: int = 0


class TodoItem(ContractModel):
    """todo 条目（REQ-005）。todo 面板数据源 = 最近一次 write_todos 工具调用的快照。"""

    content: str
    status: Literal["pending", "in_progress", "completed"]


class CompactInfo(ContractModel):
    """上下文压缩前后窗口占用（REQ-010 / AC-010-1 界面提示）。"""

    before_tokens: int
    after_tokens: int


EffortLevel = Literal["off", "low", "medium", "high"]
"""reasoning-effort 语义档位（AC-014-1）。按模型 param_support 过滤后呈现（AC-014-3）。"""

# 通用字段约定（文档性常量，供 README/生成器引用）
TIMESTAMP_FIELD = "created_at"
"""所有 wire 时间字段均为 ISO 8601 带时区字符串（UTC），由 datetime 承载。"""
