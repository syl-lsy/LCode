"""四个预置子智能体规格（REQ-016；AC-016-1；A3 §5.3）。

| 子智能体 | 模型角色 | 工具面 | 结构化 |
|---|---|---|---|
| general-purpose | roles.main | 全工具（继承主面：文件+execute+task） | 否 |
| explorer | roles.explorer | 只读（ls/read_file/glob/grep），无 write/edit/execute/task | 否 |
| coder | roles.coder | 全工具 + 沙箱 execute | 否 |
| reviewer | roles.reviewer | 只读 | `response_format=ReviewerFindings`（Q6 直用契约） |

工具面收敛口径（AC-016-3"无写工具而非仅提示词约束"的双重保证）：

- **模型侧**（本模块落实）：`ToolExclusionMiddleware` 把写类工具从模型请求中剔除
  ——模型看不见即调不了（deepagents 自带 `_ToolExclusionMiddleware` 是私有件，
  本车道在 `agent/middleware.py` 自持公开等价物）；
- **工具级 deny**（M2 不可用）：`permissions=` 参数在沙箱 backend 下被安装版
  deepagents 0.6.12 直接拒收（`middleware/filesystem.py:875` 实测
  NotImplementedError）——deepagents 支持沙箱侧 permissions 后补第二重保证。

`tools` 键显式给 `[]`：安装版 `create_sub_agent` 要求 spec 同时具备 model/tools
（缺了直接 ValueError）；空表 = 不带额外工具，内置文件工具由子智能体自己的
`FilesystemMiddleware` 注入（`graph.py:643-649` 默认栈）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from deepagents.middleware.subagents import SubAgent
from langchain_core.language_models import BaseChatModel

from app.agent.middleware import ToolExclusionMiddleware
from lcode_contracts.findings import ReviewerFindings

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

SUBAGENT_NAMES: frozenset[str] = frozenset({"general-purpose", "explorer", "coder", "reviewer"})
"""四个预置子智能体名（AUTO 规则分类器按它判"已知类型"，未知类型 fail-closed）。"""

READ_ONLY_SUBAGENTS: frozenset[str] = frozenset({"explorer", "reviewer"})
"""只读子智能体（工具面剔除写类/execute/task；AUTO 规则 R1 的判定依据）。"""

READ_ONLY_EXCLUDED: tuple[str, ...] = (
    "write_file",
    "edit_file",
    "execute",
    "task",
    "write_todos",
)
"""只读面剔除清单：写文件、命令执行、子智能体派发、todo 写入全部不可见。

`write_todos` 虽无文件副作用，但它是"写"语义的状态工具，冻结设计（A3 §5.3 表）
的 explorer/reviewer 工具面只有 ls/read_file/glob/grep——从严收敛。
"""


def load_prompt(name: str) -> str:
    """读 prompts/<name>.md（模板独立文件，不内嵌代码——A3 §1.1 目录注释）。"""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def build_subagents(
    model_factory: Callable[[str], BaseChatModel],
) -> list[SubAgent]:
    """按角色工厂构造四个子智能体规格（装配点 `assembly.build_inner_agent` 消费）。"""
    return [
        SubAgent(
            name="general-purpose",
            description=(
                "通用子智能体：继承主智能体的全部工具（文件读写、沙箱执行、子派发），"
                "适合需要完整工具面的独立子任务。"
            ),
            system_prompt=load_prompt("main"),
            model=model_factory("main"),
            tools=[],
        ),
        SubAgent(
            name="explorer",
            description=(
                "只读探索：快速浏览/检索代码回答'有什么、在哪、怎么组织'；无写文件与命令执行能力。"
            ),
            system_prompt=load_prompt("explorer"),
            model=model_factory("explorer"),
            tools=[],
            middleware=[ToolExclusionMiddleware(READ_ONLY_EXCLUDED)],
        ),
        SubAgent(
            name="coder",
            description=("编码执行：读改代码、跑沙箱命令验证（测试/构建），适合明确的实现任务。"),
            system_prompt=load_prompt("coder"),
            model=model_factory("coder"),
            tools=[],
        ),
        SubAgent(
            name="reviewer",
            description=(
                "只读审查：审查指定范围的代码，返回结构化 findings（severity/category/"
                "line 窗口），供汇总面板与扇出 reduce 机器可拼。"
            ),
            system_prompt=load_prompt("reviewer"),
            model=model_factory("reviewer"),
            tools=[],
            middleware=[ToolExclusionMiddleware(READ_ONLY_EXCLUDED)],
            response_format=ReviewerFindings,
        ),
    ]


__all__ = [
    "PROMPTS_DIR",
    "READ_ONLY_EXCLUDED",
    "READ_ONLY_SUBAGENTS",
    "SUBAGENT_NAMES",
    "build_subagents",
    "load_prompt",
]
