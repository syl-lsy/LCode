"""领域事件协议（Q5 定稿）：事件族注册表 + wire 模型。

族划分（PRD REQ-008 / AC-008-1 六类 + Q5 定稿新增的运行状态族）：

| 族 | type 字面量 | 回应需求 |
|---|---|---|
| 消息 message | message_created / message_delta / message_finished | REQ-001/003 |
| 工具 tool_call_* | tool_call_started / tool_call_finished | REQ-003/005/009/011 |
| 审批 approval_request | approval_request / approval_resolved | REQ-006/013 |
| 审查 diff_ready | diff_ready / diff_resolved | REQ-007 |
| 沙箱 sandbox_output | sandbox_output（channel: stdout/stderr/lifecycle） | REQ-002 |
| 浏览器 browser_action | browser_action（P1 占位，P0 服务端不得产出） | REQ-102 |
| 运行状态 run_status | run_status（subject: run/compact） | Q5 定稿新增 |

设计决策（详见 contracts/README.md 与 ADR-003）：
- 信封扁平：公共字段直接铺在事件对象上（seq/session_id/run_id/agent/created_at/type），
  TS 侧生成 discriminated union（按 type 判别）体验最好；
- tool_call 族 P0 冻结 started/finished 两态：参数在执行前完整到达（无参数流式），
  中间增量走 sandbox_output；预留增量属契约增量 diff；
- todo 面板数据源 = 最近一次 write_todos 的 tool_call_finished.todos 快照（六族覆盖
  REQ-005，不另立 todo 事件类型）；
- reviewer findings 走 message_finished.structured（六族覆盖 REQ-011）；
- run_status 为 Q5 定稿新增的第七族：stop/错误/压缩提示（AC-001-2/010-1、NFR-006）
  无法从六族可靠推导，作为 PRD 增量 diff 建议回填；
- 顺序保证（AC-003-4）：seq 为会话内单调递增整数（可能因事务回滚出现罕见空洞，
  只保证顺序不保证密度）；SSE id=seq，Last-Event-ID 断线续传。
"""

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import Field, model_validator
from pydantic.root_model import RootModel

from .common import AgentRef, CompactInfo, ContractModel, TodoItem, Usage
from .errors import RunErrorObj, ToolErrorObj
from .findings import ReviewerFindings


class BaseEvent(ContractModel):
    """事件信封公共字段（扁平铺开）。所有具体事件继承本类并冻结 type 字面量。"""

    seq: int = Field(ge=0, description="会话内单调递增序号；SSE id 即此值")
    session_id: str
    run_id: str | None = None
    agent: AgentRef | None = None  # null = 主智能体
    created_at: datetime


# ---------------------------------------------------------------- 消息族

class MessageCreatedEvent(BaseEvent):
    """用户消息回声：落库后、run 启动前发出（多端一致 + 纯事件回放可重建，AC-008-3）。"""

    type: Literal["message_created"] = "message_created"
    message_id: str
    role: Literal["user"] = "user"
    content: str


class MessageDeltaEvent(BaseEvent):
    """token 增量（REQ-003）。kind=thinking 对应思考折叠块（推理模型 reasoning 流）。"""

    type: Literal["message_delta"] = "message_delta"
    message_id: str
    role: Literal["assistant"] = "assistant"
    kind: Literal["text", "thinking"] = "text"
    delta: str


class StructuredResult(ContractModel):
    """结构化输出附带块（REQ-011）。status=fallback 时 data=null 且前端标注「未结构化」。"""

    schema_name: Literal["reviewer_findings"] = "reviewer_findings"
    status: Literal["ok", "fallback"] = "ok"
    data: ReviewerFindings | None = None

    @model_validator(mode="after")
    def _check_data(self) -> "StructuredResult":
        if self.status == "ok" and self.data is None:
            raise ValueError("status=ok 时必须携带 data")
        return self


class MessageFinishedEvent(BaseEvent):
    """assistant 消息收口：finish_reason + 用量 + 可选结构化结果。"""

    type: Literal["message_finished"] = "message_finished"
    message_id: str
    role: Literal["assistant"] = "assistant"
    finish_reason: Literal["stop", "length", "content_filter", "error", "cancelled"]
    usage: Usage | None = None
    structured: StructuredResult | None = None


# ---------------------------------------------------------------- 工具族

class ToolCallStartedEvent(BaseEvent):
    """工具调用开始（卡片出现：工具名 + 完整入参，AC-003-2）。"""

    type: Literal["tool_call_started"] = "tool_call_started"
    tool_call_id: str
    tool: str
    args: dict[str, Any]
    risk_level: Literal["low", "medium", "high"] | None = None


class ToolCallFinishedEvent(BaseEvent):
    """工具调用结束（卡片补全：出参摘要 + 状态，AC-003-2）。

    - error 双通道原则（调研 §4.2）：给模型仍走 ToolMessage；本事件面向 UI/落库，
      带 status/error{code,message,retryable}/truncated/duration_ms；
    - exit_code：execute 类命令退出码（非 0 不代表 status=error —— deepagents
      口径：命令跑了就是 success，读 exit_code 判断命令失败）；
    - todos：tool=write_todos 时携带全量 todo 快照（REQ-005 面板数据源）；
    - result_ref：大结果被 offload 后的沙箱内落盘引用（AC-010-2 留痕）。
    """

    type: Literal["tool_call_finished"] = "tool_call_finished"
    tool_call_id: str
    status: Literal["success", "error"]
    duration_ms: int = Field(ge=0)
    result_preview: str | None = None
    truncated: bool = False
    exit_code: int | None = None
    result_ref: str | None = None
    error: ToolErrorObj | None = None
    todos: list[TodoItem] | None = None

    @model_validator(mode="after")
    def _check_error_status(self) -> "ToolCallFinishedEvent":
        if self.status == "error" and self.error is None:
            raise ValueError("status=error 时必须携带 error 对象")
        if self.status == "success" and self.error is not None:
            raise ValueError("status=success 时不得携带 error 对象")
        return self


# ---------------------------------------------------------------- 审批族

class ApprovalRequestEvent(BaseEvent):
    """审批请求（REQ-006）：四决策 approve/edit/reject/respond 经 REST 决策端点提交。

    不超时自动放行（AC-006-5）：本事件无过期语义；挂起态跨刷新由 approvals 表恢复。
    """

    type: Literal["approval_request"] = "approval_request"
    approval_id: str
    tool_call_id: str
    tool: str
    args: dict[str, Any]  # 完整入参（命令全文），卡片必须展示原文
    risk_level: Literal["medium", "high"]
    reason: str | None = None  # 为什么需要审批（命中哪条规则）


class ApprovalResolvedEvent(BaseEvent):
    """审批决策回声（决策经 POST /approvals/{id}/decide 落库后广播）。

    用途：多标签页一致 + 纯事件回放可重建审批卡片终态（AC-008-3/AC-004-4）。
    """

    type: Literal["approval_resolved"] = "approval_resolved"
    approval_id: str
    decision: Literal["approve", "edit", "reject", "respond"]
    scope: Literal["once", "session"] = "once"
    edited_args: dict[str, Any] | None = None
    feedback: str | None = None
    decided_by: str

    @model_validator(mode="after")
    def _check_decision_payload(self) -> "ApprovalResolvedEvent":
        if self.decision == "edit" and self.edited_args is None:
            raise ValueError("decision=edit 时必须携带 edited_args（AC-006-3）")
        if self.decision == "respond" and not (self.feedback or "").strip():
            raise ValueError("decision=respond 时必须携带非空 feedback（AC-006-4）")
        return self


# ---------------------------------------------------------------- 审查族

class BaselineRef(ContractModel):
    """diff 对照基线引用（REQ-007 / AC-007-1/4）。

    kind 语义：head=输入仓库自身历史（不做 git 操作）；init=非 git 输入的
    [lcode-baseline] 初始提交（round-0）；accepted=上轮已接受后创建的基线提交。
    """

    kind: Literal["head", "init", "accepted"]
    ref: str  # tag 名（init/accepted）或 "HEAD"
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class DiffFileSummary(ContractModel):
    path: str
    old_path: str | None = None  # rename 时的原路径
    change: Literal["added", "modified", "deleted", "renamed"]
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    binary: bool = False


class DiffReadyEvent(BaseEvent):
    """diff 就绪（REQ-007）：摘要走事件，逐文件完整 patch 走 REST（patch_ref）。

    新建文件须纳入 diff（git add -N 口径，AC-007-1）；totals 与 files 求和一致
    是诚实性强校验（F15：完成态呈现以 diff_ready 事实为准）。
    """

    type: Literal["diff_ready"] = "diff_ready"
    review_id: str
    round: int = Field(ge=1)
    baseline: BaselineRef
    files: list[DiffFileSummary] = Field(min_length=1)
    additions_total: int = Field(ge=0)
    deletions_total: int = Field(ge=0)
    patch_ref: str  # 如 "/api/v1/diff-reviews/{id}"

    @model_validator(mode="after")
    def _check_totals(self) -> "DiffReadyEvent":
        if self.additions_total != sum(f.additions for f in self.files):
            raise ValueError("additions_total 必须等于各文件 additions 之和")
        if self.deletions_total != sum(f.deletions for f in self.files):
            raise ValueError("deletions_total 必须等于各文件 deletions 之和")
        return self


class DiffResolvedEvent(BaseEvent):
    """diff 裁决回声（AC-007-3）：accepted 后端创建基线提交成为下一轮对照（AC-007-4）。"""

    type: Literal["diff_resolved"] = "diff_resolved"
    review_id: str
    round: int = Field(ge=1)
    verdict: Literal["accepted", "rejected"]
    feedback: str | None = None
    decided_by: str


# ---------------------------------------------------------------- 沙箱族

class SandboxOutputEvent(BaseEvent):
    """沙箱输出（REQ-002）：
    - channel=stdout/stderr：命令输出增量（终端标签实时滚动）。exec_id 关联发起命令
      的工具调用（execute 类工具的 exec_id = tool_call_id；命令与退出码经
      tool_call_finished.exit_code 补全）；
    - channel=lifecycle：沙箱生命周期（phase: creating/running/paused/resumed/
      killed/crashed/recreated）——「沙箱暂停/恢复」状态可见性的载体（PRD §9）。
    """

    type: Literal["sandbox_output"] = "sandbox_output"
    sandbox_id: str
    exec_id: str | None = None
    channel: Literal["stdout", "stderr", "lifecycle"]
    data: str = ""
    phase: (
        Literal["creating", "running", "paused", "resumed", "killed", "crashed", "recreated"] | None
    ) = None
    reason: str | None = None
    provider: Literal["bailian", "opensandbox"] | None = None

    @model_validator(mode="after")
    def _check_channel(self) -> "SandboxOutputEvent":
        if self.channel == "lifecycle":
            if self.phase is None:
                raise ValueError("channel=lifecycle 时必须携带 phase")
        elif self.phase is not None:
            raise ValueError("phase 仅在 channel=lifecycle 时允许出现")
        if self.channel in ("stdout", "stderr") and self.exec_id is None:
            raise ValueError("stdout/stderr 输出必须携带 exec_id（终端标注命令来源）")
        return self


# ---------------------------------------------------------------- 运行状态族（Q5 定稿新增）

class RunStatusEvent(BaseEvent):
    """运行/压缩状态（Q5 定稿新增族）：

    - subject=run：phase started/finished/stopped/error（停止与错误路径的 UI 状态机
      信号，AC-001-2 / NFR-006；无法从消息族可靠推导）；
    - subject=compact：phase started/finished（上下文压缩提示，AC-010-1），
      finished 时必须带 compact 前后窗口占用。
    """

    type: Literal["run_status"] = "run_status"
    subject: Literal["run", "compact"]
    phase: Literal["started", "finished", "stopped", "error"]
    error: RunErrorObj | None = None
    usage_totals: Usage | None = None  # subject=run, phase=finished 时的 run 级汇总
    compact: CompactInfo | None = None

    @model_validator(mode="after")
    def _check_subject_payload(self) -> "RunStatusEvent":
        if self.subject == "run" and self.phase == "error" and self.error is None:
            raise ValueError("subject=run 且 phase=error 时必须携带 error")
        if self.subject == "compact" and self.phase == "finished" and self.compact is None:
            raise ValueError("subject=compact 且 phase=finished 时必须携带 compact（前后窗口占用）")
        if self.subject == "compact" and self.phase in ("stopped",):
            raise ValueError("subject=compact 不存在 stopped 相位")
        return self


# ---------------------------------------------------------------- 浏览器族（P1 占位）

class BrowserActionEvent(BaseEvent):
    """浏览器动作（REQ-102 / 场景 F）。P1 占位：P0 服务端不得产出本事件；
    schema 先行冻结以稳定契约，前端 P0 不实现消费端。
    """

    type: Literal["browser_action"] = "browser_action"
    action: str
    url: str | None = None
    status: Literal["started", "finished", "error"]
    error: ToolErrorObj | None = None
    screenshot_ref: str | None = None


# ---------------------------------------------------------------- 注册表与联合类型

_EVENT_CLASSES: list[type[BaseEvent]] = [
    MessageCreatedEvent,
    MessageDeltaEvent,
    MessageFinishedEvent,
    ToolCallStartedEvent,
    ToolCallFinishedEvent,
    ApprovalRequestEvent,
    ApprovalResolvedEvent,
    DiffReadyEvent,
    DiffResolvedEvent,
    SandboxOutputEvent,
    RunStatusEvent,
    BrowserActionEvent,
]

EVENT_TYPES: dict[str, type[BaseEvent]] = {c.model_fields["type"].default: c for c in _EVENT_CLASSES}
"""type 字面量 -> 模型类。harness 校验注册表与 fixtures 一一对应。"""

Event = Annotated[Union[tuple(_EVENT_CLASSES)], Field(discriminator="type")]
"""带判别器的校验用联合类型（harness/服务端入口用 TypeAdapter(Event)）。"""

EventUnion = RootModel[Union[tuple(_EVENT_CLASSES)]]
"""无判别器联合（仅用于导出 JSON Schema 给 TS 生成 anyOf 联合类型）。"""

P0_EVENT_TYPES: set[str] = set(EVENT_TYPES) - {"browser_action"}
"""P0 服务端允许产出的事件集合（browser_action 为 P1 占位）。"""
