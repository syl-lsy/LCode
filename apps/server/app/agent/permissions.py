"""REQ-013 权限规则表 → deepagents `FilesystemPermission` / `InterruptOnConfig` 生成器（A3 §5.4）。

**M3 接线（已落地）**：`assembly.build_inner_agent(..., approval_mode=...)` 把
`build_interrupt_on(mode=...)` 传给 `create_deep_agent(interrupt_on=...)`——安装版
deepagents 0.6.12 的主图 tail 自动装 `HumanInTheLoopMiddleware`，声明式子智能体自动
继承同一策略（`graph.py:830-835` / `graph.py:686`，m3-spikes §1.2 实测）。审批是
**唯一闸门**（AGENTS.md 硬约束 11）：任何工具只要命中 interrupt 面就必须人工放行，
没有"自动放行"的旁路。

`permissions=` 参数仍不可用：沙箱 backend 下 `FilesystemMiddleware` 直接拒收
（`deepagents/middleware/filesystem.py:875` NotImplementedError，本车道实测），
直到 deepagents 实现沙箱侧 permissions（升级时重读 `graph.py` 确认）。

两种审批模式（REQ-013 / PRD L307）：

- `manual`（默认）：`APPROVAL_REQUIRED_TOOLS` 全量 + fs 写类工具进 interrupt 面；
- `auto`：同一张表，但 `AUTO_ELIGIBLE` 内的工具改挂 `_auto_decision_when`——**仍在
  interrupt 面**，谓词只读 AUTO 分类器写进状态的决策（`AUTO_APPROVAL_STATE_KEY`）：
  无决策 / 决策 false → interrupt（fail-closed）；决策 true → 免审。分类器本身在
  `agent/auto_approval.py` 的 model-call 后置钩子里跑（决策随 checkpoint 存活，
  resume 不重新分类）。**沙箱红线**（execute/write_file/edit_file）在两种模式下恒在
  interrupt 面：`SANDBOX_REDLINE_TOOLS` 与 `AUTO_ELIGIBLE` 的交集必须在构造期为空，
  否则直接抛错（结构保证，不靠注释）。

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
from collections.abc import Mapping
from typing import Any, Final, Literal

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

ApprovalMode = Literal["manual", "auto"]
"""审批模式（sessions.approval_mode 同源；见 build_interrupt_on）。"""

APPROVAL_MODES: Final[frozenset[str]] = frozenset({"manual", "auto"})
"""合法模式集合（未知值在 build_interrupt_on 处 fail loudly，不静默回落）。"""

AUTO_ELIGIBLE: Final[frozenset[str]] = frozenset({"task"})
"""AUTO 模式下交给分类器决策的工具集（M3 仅 task；M4 纳 MCP 只读工具）。

`auto_approval.AutoApprovalMiddleware` 对这些工具调用分类，把决策按 tool_call_id
写进内层图 state（键名 `AUTO_APPROVAL_STATE_KEY`）；`_auto_decision_when` 只读该
决策决定是否 interrupt。分类器不可用/低置信/异常时决策为 false → 仍走人工审批
（fail-closed）。**该集合永远不得包含沙箱红线工具**（构造期校验）。
"""

AUTO_APPROVAL_STATE_KEY: Final[str] = "auto_approval_decisions"
"""AUTO 分类决策的 state 字段名（middleware state_schema ↔ when 谓词的唯一契约）。

值是 `{tool_call_id: {"auto_approve": bool, "reason": str, "tool": str}}`；随内层图
checkpoint 持久化，resume 时 HITL 的 `when` 读到同一份决策（m3-plan 风险 3）。
"""

SANDBOX_REDLINE_TOOLS: Final[frozenset[str]] = frozenset({"execute", "write_file", "edit_file"})
"""沙箱红线：两种审批模式下都必须经过人工审批的工具（ADR-002 / PRD 风险表）。

execute / write_file / edit_file 会真正改动宿主/sandbox 状态；AUTO 至多放行"安全
非沙箱集"（当前 = task），沙箱工具一律人工确认——本集合与 `AUTO_ELIGIBLE` 的交集
在 `build_interrupt_on` 构造期强制为空。
"""

RISK_LEVELS: Final[tuple[str, ...]] = ("medium", "high")
"""契约 `ApprovalItem.risk_level` 的值域（只两档：审批面里没有 low）。"""

_HIGH_RISK_TOOLS: Final[frozenset[str]] = frozenset({"execute"})
"""高风险工具：命令执行可改变沙箱状态且参数面不可穷举（AC-013-2 门控只缩小触发面）。"""

_MEDIUM_RISK_TOOLS: Final[frozenset[str]] = frozenset({"write_file", "edit_file", "task"})
"""中风险工具：文件写与子智能体派发（参数由卡片展示，人工确认成本低）。"""

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


def _auto_decision_when(request: ToolCallRequest) -> bool:
    """AUTO 工具（task）的 `when`：只读已存分类决策，绝不做 IO / 调分类器。

    - 无 state / 无该 tool_call 的决策 / 决策畸形 → True（**interrupt，fail-closed**）；
    - 已存 `auto_approve=False` → True（分类器拒绝或故障，走人工）；
    - 已存 `auto_approve=True` → False（免审放行）。

    state 来自 checkpoint（决策由 `AutoApprovalMiddleware` 在 model-call 后置钩子里
    写入，早于 HITL 的 after_model）——同一 tool_call 在 resume 重放时读到同一份决策，
    因此不会出现「resume 时 when 翻转」（m3-plan 风险 3）。
    """
    decision = _stored_decision(request, str(request.tool_call.get("id") or ""))
    return not bool(decision and decision.get("auto_approve"))


def _stored_decision(request: ToolCallRequest, tool_call_id: str) -> Mapping[str, Any] | None:
    """从请求 state 取该调用的决策（任何非 dict / 非 bool 形态一律视为无决策）。"""
    state = getattr(request, "state", None)
    if not tool_call_id or not isinstance(state, Mapping):
        return None
    decisions = state.get(AUTO_APPROVAL_STATE_KEY)
    if not isinstance(decisions, Mapping):
        return None
    decision = decisions.get(tool_call_id)
    if not isinstance(decision, Mapping) or not isinstance(decision.get("auto_approve"), bool):
        return None
    return decision


# ---------------------------------------------------------------- risk / reason（审批卡片）

RiskLevel = Literal["medium", "high"]


def risk_level_for(tool: str, args: Mapping[str, Any] | None = None) -> RiskLevel | None:
    """工具 → 契约风险等级（AC-006-1 卡片展示；`None` = 不在审批面）。

    - execute → high（命令执行可写任意文件/起外部进程，参数面不可静态穷举）；
    - write_file / edit_file / task → medium；
    - 只读工具（ls/read_file/glob/grep）与未知工具 → None（它们本就不进审批面；
      若未知工具意外出现在 interrupt 载荷里，调用方按 medium 兜底，见 translator）。
    """
    name = str(tool or "")
    if name in _HIGH_RISK_TOOLS:
        return "high"
    if name in _MEDIUM_RISK_TOOLS:
        return "medium"
    return None


def approval_reason(tool: str, args: Mapping[str, Any] | None = None) -> str:
    """工具 → 确定性中文审批理由（说明命中哪条规则；AC-006-5 恢复卡片必须展示）。

    纯函数、无外部依赖（不查模型/配置），同一输入恒同文案——事件与单测可稳定断言。
    文案只描述规则命中，不回显命令全文以外的新信息（命令全文本来就在卡片 args 里）。
    """
    name = str(tool or "")
    if name == "execute":
        command = (args or {}).get("command")
        if isinstance(command, str) and command.strip():
            return "沙箱命令执行：命令不在只读白名单内，命中 REQ-013 沙箱红线"
        return "沙箱命令执行：无法判定命令安全性（缺参或非字符串），按 fail-closed 拦截"
    if name == "write_file":
        return "文件写入：命中文件写审批规则（write_file）"
    if name == "edit_file":
        return "文件修改：命中文件写审批规则（edit_file）"
    if name == "task":
        return "子智能体派发：默认审批表要求人工放行（task）"
    return f"工具 {name or '<unknown>'} 不在免审批白名单内，默认需要人工确认"


# ---------------------------------------------------------------- interrupt_on 生成


def build_interrupt_on(
    rules: list[FilesystemPermission] | None = None,
    *,
    mode: ApprovalMode | str = "manual",
) -> dict[str, InterruptOnConfig]:
    """把默认规则表编译为 `create_deep_agent(interrupt_on=...)` 的映射（M3 接线点）。

    组成（A3 §5.4）：
    - fs 面：由 deepagents 自带的 `_build_interrupt_on_from_permissions` 从规则表生成
      （带路径级 `when` 谓词）；其中 write_file/edit_file 属红线，随后被**强制覆盖**为
      「每次调用都打断」（规则表只能加严、不能收窄）；
    - `execute`：参数级门控（白名单命令放行），**两种模式恒在面内**；
    - `task`：MANUAL 恒 interrupt；AUTO 下挂 `_auto_decision_when`——仍在面内，按
      分类器写进 state 的决策决定是否打断（无决策/false → interrupt，fail-closed）。

    自定义 `rules` 时，fs 面按传入规则生成（子智能体覆写父规则的场景），**但红线三
    工具（execute/write_file/edit_file）无视规则强制并集写入**：`_build_interrupt_on_
    from_permissions` 只在存在 interrupt 模式规则时产出条目，若调用方传入只含 allow
    的规则（如 `[read allow, write allow]`），写类工具会从审批面消失——本函数用
    `_always_when` 覆盖 write_file/edit_file 的条目，把"红线恒在面内"从注释升格为
    结构保证（安全审计 P3-1）。execute 保留 AC-013-2 的参数级门控（只读白名单命令
    免审批），门控本身对任何 rules 都生效，不存在被规则掏空的路径。
    `mode` 只认 `manual|auto`，其余值抛 ValueError（配置/DB CHECK 双源，不静默回落）。

    沙箱红线（不变量）：`SANDBOX_REDLINE_TOOLS ∩ AUTO_ELIGIBLE` 必须为空；非空即抛
    RuntimeError——把"红线不可被 AUTO 放行"钉成构造期断言，而不是注释约定。
    """
    if mode not in APPROVAL_MODES:
        raise ValueError(f"未知审批模式：{mode!r}（只允许 manual / auto）")
    if SANDBOX_REDLINE_TOOLS & AUTO_ELIGIBLE:  # pragma: no cover —— 常量被改错时立即暴露
        raise RuntimeError(
            f"沙箱红线工具不得进 AUTO_ELIGIBLE：{sorted(SANDBOX_REDLINE_TOOLS & AUTO_ELIGIBLE)}"
        )

    fs_entries = _build_interrupt_on_from_permissions(
        rules if rules is not None else default_permissions()
    )
    entries: dict[str, InterruptOnConfig] = dict(fs_entries)
    # 红线并集（结构保证）：execute 用参数级门控；write_file/edit_file 每次调用都打断
    # （规则表只能加严，不能收窄——write 的路径级 when 谓词在此一律被忽略）。
    entries["execute"] = InterruptOnConfig(allowed_decisions=ALLOWED_DECISIONS, when=_execute_when)
    entries["write_file"] = InterruptOnConfig(
        allowed_decisions=ALLOWED_DECISIONS, when=_always_when
    )
    entries["edit_file"] = InterruptOnConfig(allowed_decisions=ALLOWED_DECISIONS, when=_always_when)
    # 审批面并集（setdefault：已由上面红线/fs 规则定义的条目不被覆盖）。
    # - MANUAL：`APPROVAL_REQUIRED_TOOLS` 全量恒打断（execute 已在上面按参数门控装配）；
    # - AUTO：`AUTO_ELIGIBLE` 工具仍在面内，谓词只读分类器写进 state 的决策（无决策/
    #   false → 打断）。沙箱红线由构造期不变量保证不落进 AUTO_ELIGIBLE；万一被误改，
    #   setdefault 也保留上面的红线条目（永不降级为分类器谓词）。
    if mode == "auto":
        for tool in AUTO_ELIGIBLE:
            entries.setdefault(
                tool,
                InterruptOnConfig(allowed_decisions=ALLOWED_DECISIONS, when=_auto_decision_when),
            )
    else:
        for tool in APPROVAL_REQUIRED_TOOLS:
            entries.setdefault(
                tool, InterruptOnConfig(allowed_decisions=ALLOWED_DECISIONS, when=_always_when)
            )
    return entries


__all__ = [
    "ALLOWED_DECISIONS",
    "APPROVAL_MODES",
    "APPROVAL_REQUIRED_TOOLS",
    "AUTO_APPROVAL_STATE_KEY",
    "AUTO_ELIGIBLE",
    "ApprovalMode",
    "READ_ONLY_TOOLS",
    "RISK_LEVELS",
    "RiskLevel",
    "SAFE_COMMANDS",
    "SANDBOX_REDLINE_TOOLS",
    "WRITE_TOOLS",
    "approval_reason",
    "build_interrupt_on",
    "command_needs_approval",
    "default_permissions",
    "risk_level_for",
]
