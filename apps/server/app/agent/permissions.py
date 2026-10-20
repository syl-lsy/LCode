"""REQ-013 权限规则表 → deepagents `FilesystemPermission` / `InterruptOnConfig` 生成器（A3 §5.4）。

**M2 边界（车道红线）**：本模块只提供规则与生成器 + 单测；装配（assembly.py）
**不**把它们传给 `create_deep_agent`——HITL 是 M3 范围。且这不是单纯"未排期"：
安装版 deepagents 0.6.12 的 `FilesystemMiddleware` 在提供命令执行的（沙箱）backend
下直接拒收 permissions 参数（`deepagents/middleware/filesystem.py:875`
NotImplementedError，本车道实测）。因此：

- M2/M3 的 `create_deep_agent(..., interrupt_on=build_interrupt_on())` 通路成立
  （interrupt_on 不经 FilesystemMiddleware，能装 HITL——M3 接线点）；
- `permissions=` 参数在沙箱 backend 下永远不可用，直到 deepagents 实现沙箱侧
  permissions（升级时重读 `graph.py` 确认）。

规则语义（deepagents 口径）：规则按声明序评估、首条命中即止、无命中默认放行；
`FilesystemMiddleware` 在工具级执行 allow/deny；interrupt 语义经
`interrupt_on` → `HumanInTheLoopMiddleware`（唯一审批闸门，AGENTS.md 硬约束 11）。

AC-013-2 参数级门控（fail-closed）：`execute` 的 `args.command` 只有在**严格可判定
为只读单命令**时才放行（`when` 谓词返回 False），其余一律 interrupt：

- 首 token 大小写敏感精确命中 `SAFE_COMMANDS`（`cat` / `ls` / `pwd` / `which`）；
  `echo`（可重定向持久化到 shell 启动文件）与全部 `git` 子命令（`git diff --output=`
  任意写、`--ext-diff` 执行外部程序）已移出白名单；
- 命令中不得出现换行 / 回车 / NUL（命令分隔与截断风险）以及 `$(` / 反引号（命令替换）；
- 用 `shlex`（posix + punctuation）分词，出现 `;` `&` `|` `<` `>` `(` `)` 及其组合
  token（`;;` `&&` `||` `|&` `>>` `<<` 等）即拒绝；
- 分词解析失败（`ValueError`）同样拒绝。

参数不做进一步白名单（如 `cat` 的路径），但上述元字符/替换检查已阻断串联与写重定向。
无法判定（缺参、非字符串、空串）一律拦截——审批闸门只能偏保守。
"""

from __future__ import annotations

import shlex
from typing import Any, Final

from deepagents.middleware._fs_interrupt import _build_interrupt_on_from_permissions
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware import InterruptOnConfig
from langchain.agents.middleware.human_in_the_loop import DecisionType
from langchain.tools.tool_node import ToolCallRequest

READ_ONLY_TOOLS: Final[tuple[str, ...]] = ("ls", "read_file", "glob", "grep")
"""只读工具面（免审批；AC-013-5 explorer 双重保证之一；translator 幂等白名单同源）。"""

WRITE_TOOLS: Final[tuple[str, ...]] = ("write_file", "edit_file")
"""文件写类工具（默认表：interrupt）。"""

APPROVAL_REQUIRED_TOOLS: Final[tuple[str, ...]] = ("execute", "task")
"""非 fs 的审批面：execute（命令执行）与 task（子智能体派发，REQ-013 默认表）。"""

ALLOWED_DECISIONS: Final[list[DecisionType]] = ["approve", "edit", "reject", "respond"]
"""审批四决策（安装版 `_build_interrupt_on_from_permissions` 的同款集合，M3 对表 U3）。"""

SAFE_COMMANDS: Final[frozenset[str]] = frozenset({"cat", "ls", "pwd", "which"})
"""AC-013-2 的 execute 只读命令白名单：首 token 大小写敏感精确匹配。

只收无副作用、不写文件、不执行外部程序的命令。`echo` 与全部 `git` 子命令显式排除
（见模块 docstring）。参数不做进一步白名单；串联/重定向/替换由 `command_needs_approval`
的元字符检查拦截。
"""

_FORBIDDEN_CONTROL_CHARS: Final[tuple[str, ...]] = ("\n", "\r", "\x00")
"""命令分隔/截断风险字符：换行、回车、NUL。出现即拒绝（fail-closed）。"""

_SHELL_METACHARACTERS: Final[frozenset[str]] = frozenset(";&|<>()")
"""shell 控制/重定向/子 shell 元字符；任何纯由这些字符组成的 token 一律拒绝。"""


# ---------------------------------------------------------------- 规则表


def default_permissions() -> list[FilesystemPermission]:
    """REQ-013 默认规则表：只读显式放行；文件写类 interrupt（首条命中即止）。

    规则语义：read 命中第一条（allow）；write 跳过第一条（操作不匹配）落到
    第二条（interrupt）→ `_build_interrupt_on_from_permissions` 由此生成
    write_file / edit_file 的 interrupt 面。execute / task 的审批面不属 fs 规则，
    在 `build_interrupt_on` 里单独装配。

    M2 注意：**本规则表不可传给 `create_deep_agent(permissions=...)`**——含
    interrupt 模式会自动装配 HITL（M3），且沙箱 backend 下 permissions 参数本身
    被安装版拒收（filesystem.py:875）。规则表在 M2 只作为 interrupt_on 生成与
    审批策略的可审计文档存在。
    """
    return [
        FilesystemPermission(operations=["read"], paths=["/**"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/**"], mode="interrupt"),
    ]


# ---------------------------------------------------------------- 命令门控（AC-013-2）


def _tokenize(command: str) -> list[str] | None:
    """按 shell 词法分词；解析失败返回 None（调用方 fail-closed 处理）。

    `punctuation_chars=True` 把 `;` `&&` `>>` 等连续元字符合并为独立 token；
    `commenters=""` 关闭 `#` 注释语义，避免注释吞掉后续 token 造成检查盲区。
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def command_needs_approval(command: Any) -> bool:
    """execute 的 `args.command` 是否需要审批（fail-closed）。

    仅当命令严格可判定为白名单内的只读单命令时返回 False：
    1. 不含换行/回车/NUL、`$(`、反引号；
    2. shlex 分词成功，且没有任何纯 shell 元字符 token（`;` `&&` `|` `>` `<<` 等）；
    3. 首 token 大小写敏感精确命中 `SAFE_COMMANDS`。

    缺参 / 非字符串 / 空串 / 解析失败一律 True（无法判定即拦截）。
    """
    if not isinstance(command, str) or not command.strip():
        return True
    if any(char in command for char in _FORBIDDEN_CONTROL_CHARS):
        return True
    # 命令替换在引号内也会被 shell 执行；不做引号上下文豁免，一律拒绝。
    if "$(" in command or "`" in command:
        return True

    tokens = _tokenize(command)
    if not tokens:
        return True
    if any(token and all(char in _SHELL_METACHARACTERS for char in token) for token in tokens):
        return True
    return tokens[0] not in SAFE_COMMANDS


def _execute_when(request: ToolCallRequest) -> bool:
    return command_needs_approval(request.tool_call.get("args", {}).get("command"))


def _always_when(request: ToolCallRequest) -> bool:  # noqa: ARG001
    return True


# ---------------------------------------------------------------- interrupt_on 生成


def build_interrupt_on(
    rules: list[FilesystemPermission] | None = None,
) -> dict[str, InterruptOnConfig]:
    """把默认规则表编译为 `create_deep_agent(interrupt_on=...)` 的映射（M3 接线点）。

    组成（A3 §5.4）：
    - fs 写类工具（write_file/edit_file）：由 deepagents 自带的
      `_build_interrupt_on_from_permissions` 从规则表生成（带路径级 `when` 谓词）；
    - `execute`：参数级门控（白名单命令放行）；
    - `task`：子智能体派发恒 interrupt（默认表；explorer 免审批语义由其自身的
      只读工具面承担，M3 若放行 explorer 派发再改此表）。

    自定义 `rules` 时，fs 面按传入规则生成（子智能体覆写父规则的场景）。
    """
    fs_entries = _build_interrupt_on_from_permissions(
        rules if rules is not None else default_permissions()
    )
    entries: dict[str, InterruptOnConfig] = {
        **fs_entries,
        "execute": InterruptOnConfig(allowed_decisions=ALLOWED_DECISIONS, when=_execute_when),
        "task": InterruptOnConfig(allowed_decisions=ALLOWED_DECISIONS, when=_always_when),
    }
    return entries


__all__ = [
    "ALLOWED_DECISIONS",
    "APPROVAL_REQUIRED_TOOLS",
    "READ_ONLY_TOOLS",
    "SAFE_COMMANDS",
    "WRITE_TOOLS",
    "build_interrupt_on",
    "command_needs_approval",
    "default_permissions",
]
