"""REST + SSE API 契约（DTO + 端点注册表）。

响应信封（对齐全局 python-backend-base 基座铁律 9，A3 详设落地）：
- 成功侧：/api/ 前缀统一 `{"code":"0","message":"ok"|null,"data":<DTO>}`；
- 错误侧：`{"code":"<RestErrorCode>","message":"<人话>"}`（errors.ErrorBody）；
- SSE 事件流不走信封（裸事件 JSON，见 events.py）。

SSE 传输语义（GET /api/v1/sessions/{session_id}/events）：
- 帧格式：`id: <seq>` / `event: <type>` / `data: <单行 JSON>`；
- 连接建立即发 `retry: 3000`；每 15s 发注释心跳 `: keep-alive`；
- 断线续传：优先 `Last-Event-ID` 请求头，其次 `?after_seq=` 查询参数；
  服务端先从事件库重放 seq > 水位的持久化事件，再切实时流（事件先落库后广播，
  无缝）；AC-003-3「已收到内容不丢」由重放保证；
- 流前错误（会话不存在等）以普通 REST 错误体返回；流中业务错误走 run_status 事件。

分页约定：游标分页（next_cursor 为空表示尾页），不做 offset 深翻页；游标端点 =
messages / approvals 列表（会话列表为全量 + total，无游标——列表小，检索靠 title 子串）。
ID 约定：服务端生成的 id 为 UUID 字符串；wire 层不携带前缀。
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from .common import ContractModel, EffortLevel, Usage
from .errors import RestErrorCode
from .events import BaselineRef, DiffFileSummary, StructuredResult

SandboxProvider = Literal["bailian", "opensandbox"]
ApprovalMode = Literal["manual", "auto"]
RunState = Literal["idle", "running", "waiting_approval"]


# ---------------------------------------------------------------- 会话

class SessionCreate(ContractModel):
    title: str | None = None
    model: str | None = None
    reasoning_effort: EffortLevel | None = None
    approval_mode: ApprovalMode | None = None


class SessionPatch(ContractModel):
    title: str | None = None
    model: str | None = None
    reasoning_effort: EffortLevel | None = None
    approval_mode: ApprovalMode | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "SessionPatch":
        if not self.model_fields_set:
            raise ValueError("至少提供一个更新字段")
        return self


class SessionDetail(ContractModel):
    id: str
    title: str
    status: Literal["active", "archived"]
    model: str
    reasoning_effort: EffortLevel
    approval_mode: ApprovalMode
    sandbox_provider: SandboxProvider
    run_state: RunState  # 刷新恢复的关键状态（AC-006-5：waiting_approval 可续操作）
    created_at: datetime
    updated_at: datetime
    last_active_at: datetime


class SessionPage(ContractModel):
    items: list[SessionDetail]
    total: int = Field(ge=0)


# ---------------------------------------------------------------- 消息与运行

class MessageCreate(ContractModel):
    content: str = Field(min_length=1)

    @model_validator(mode="after")
    def _non_blank(self) -> "MessageCreate":
        if not self.content.strip():
            raise ValueError("content 不能为空白（AC-001-4）")
        return self


class MessageSendAck(ContractModel):
    message_id: str
    run_id: str
    event_seq: int = Field(ge=0)
    """当前会话事件水位：客户端立即以 ?after_seq=event_seq 订阅 SSE，落差由重放补齐。"""


class MessageItem(ContractModel):
    """消息历史投影（GET /sessions/{id}/messages，供导出/检索；面板重建走事件流）。

    不含 thinking：历史导出不携带思考内容（回放路径有——message_delta(kind=thinking)
    事件流；messages 表 thinking 列仍照常落库），本投影取最小面。
    """

    id: str
    role: Literal["user", "assistant"]
    content: str
    agent_id: str | None = None
    run_id: str | None = None
    created_at: datetime
    finish_reason: str | None = None
    structured: StructuredResult | None = None


class MessagePage(ContractModel):
    items: list[MessageItem]
    next_cursor: str | None = None


class StopRunAck(ContractModel):
    run_id: str
    status: Literal["stopped"]  # 幂等：对已停止 run 重复 stop 返回 200 + stopped


# ---------------------------------------------------------------- 审批

class ApprovalItem(ContractModel):
    """审批行投影（GET /approvals 列表/详情；与 approvals 表逐值对应）。

    - status 含 auto_approved：AUTO 模式分类器决策的落行（decided_by='classifier'，
      审计面见 contracts/README「表规格补充语义」/ REQ-013 AC-013-3）；
    - reason：命中哪条规则/分类器理由——pending 卡片恢复（AC-006-5）必须展示，
      故进 REST 投影；
    - 其余决策载荷（edited_args/feedback）以 approval_resolved 事件回放为准，
      不重复投影（列表仅承载卡片恢复与筛选所需字段）。
    """

    id: str
    session_id: str
    tool: str
    args: dict[str, Any]
    risk_level: Literal["medium", "high"]
    status: Literal["pending", "approved", "rejected", "edited", "responded", "auto_approved"]
    decision: Literal["approve", "edit", "reject", "respond"] | None = None
    scope: Literal["once", "session"] = "once"
    reason: str | None = None
    created_at: datetime
    decided_at: datetime | None = None


class ApprovalPage(ContractModel):
    items: list[ApprovalItem]
    next_cursor: str | None = None


class ApprovalDecisionRequest(ContractModel):
    """四决策（REQ-006）。edit=改写参数后放行（AC-006-3）；respond=人工反馈（AC-006-4）。

    scope=session 对应原型「完全访问/始终允许」的会话级持续免审（R9）；P0 落地为
    会话 approval_mode 升级，权限规则持久化属 P1 权限 API。
    """

    decision: Literal["approve", "edit", "reject", "respond"]
    scope: Literal["once", "session"] = "once"
    edited_args: dict[str, Any] | None = None
    feedback: str | None = None

    @model_validator(mode="after")
    def _check_payload(self) -> "ApprovalDecisionRequest":
        if self.decision == "edit" and self.edited_args is None:
            raise ValueError("decision=edit 时必须携带 edited_args")
        if self.decision == "respond" and not (self.feedback or "").strip():
            raise ValueError("decision=respond 时必须携带非空 feedback")
        return self


class ApprovalDecisionAck(ContractModel):
    approval: ApprovalItem
    run_id: str | None = None  # 被 resume 的 run；若 run 已不存在（如会话被删）为 null


# ---------------------------------------------------------------- diff 审查

class DiffReviewDetail(ContractModel):
    id: str
    session_id: str
    run_id: str | None = None
    round: int = Field(ge=1)
    verdict: Literal["pending", "accepted", "rejected"]
    baseline: BaselineRef
    files: list[DiffFileSummary]
    additions_total: int = Field(ge=0)
    deletions_total: int = Field(ge=0)
    feedback: str | None = None
    decided_by: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None


class DiffFileDetail(ContractModel):
    path: str
    change: Literal["added", "modified", "deleted", "renamed"]
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    binary: bool = False
    patch: str | None = None  # unified diff 文本；binary 文件为 null（走 blob_ref）
    blob_ref: str | None = None


class DiffVerdictRequest(ContractModel):
    verdict: Literal["accepted", "rejected"]
    feedback: str | None = None  # 驳回附意见（AC-007-3；accepted 时忽略）


# ---------------------------------------------------------------- 模型与成本

class ModelInfo(ContractModel):
    id: str
    display_name: str
    provider: str
    supports_reasoning: bool
    reasoning_efforts: list[EffortLevel]  # 按 param_support 过滤后的可选档位（AC-014-3 置灰依据）
    is_default: bool = False


class ModelList(ContractModel):
    models: list[ModelInfo]
    default_model: str


class CostSummaryRow(ContractModel):
    group_key: str  # dimension=session 时为 session_id；date 时为 YYYY-MM-DD；model 时为模型 id
    model_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)  # 思考 token 计入本口径（PRD 硬约束 9）
    total_tokens: int = Field(ge=0)
    cost: float = Field(ge=0.0)  # 估算值（元），落库精度 numeric(12,6)
    currency: str = "CNY"


class CostSummary(ContractModel):
    dimension: Literal["session", "date", "model"]
    rows: list[CostSummaryRow]


class SandboxCostEstimate(ContractModel):
    rate_per_hour: float  # 运行口径 1C2G ≈ 0.156 元/小时（AC-015-4）
    accrued: float  # 本会话累计估算（运行 + 暂停保留）
    currency: str = "CNY"


class SessionCost(ContractModel):
    model: Usage
    model_cost: float = Field(ge=0.0)
    sandbox: SandboxCostEstimate


class SandboxStatus(ContractModel):
    provider: SandboxProvider
    sandbox_id: str | None = None
    phase: Literal["none", "creating", "running", "paused", "killed", "crashed"] = Field(
        description=(
            "当前状态投影（查询面）。事件侧 SandboxOutputEvent.phase 的迁移动作在此折叠："
            "resumed/recreated → running（动作不是稳态）；none = 尚无沙箱实例。"
            "两枚举差异为有意设计——事件记迁移（含动作），状态只表达稳态"
        )
    )
    running_since: datetime | None = None
    paused_since: datetime | None = None
    cost_estimate: SandboxCostEstimate | None = None


# ---------------------------------------------------------------- 端点注册表

class QueryParamSpec(ContractModel):
    """查询参数规格（wire 名 / 语义类型 / 必填 / 枚举值域）。

    前端 URL 构造与后端解析的唯一契约面；参数名冻结后变更走增量 diff。
    仅建模查询参数（query string）；路径参数由 path 模板（{...}）承载。
    """

    name: str
    type: Literal["string", "int", "enum"]
    required: bool = False
    enum: list[str] | None = None  # 仅 type=enum：非空值域
    description: str = ""

    @model_validator(mode="after")
    def _check_enum(self) -> "QueryParamSpec":
        if self.type == "enum" and not self.enum:
            raise ValueError("type=enum 时必须给出非空 enum 值域")
        if self.type != "enum" and self.enum is not None:
            raise ValueError("enum 值域仅在 type=enum 时允许出现")
        return self


class EndpointSpec(ContractModel):
    method: Literal["GET", "POST", "PATCH", "DELETE"]
    path: str
    summary: str
    request: str | None = None   # DTO 名（REST_DTOS 键）
    response: str | None = None  # DTO 名（REST_DTOS 键）；SSE 端点为 null
    sse: bool = False
    query: list[QueryParamSpec] = Field(default_factory=list)  # 查询参数（名/型/必填/值域）
    errors: list[RestErrorCode] = Field(default_factory=list)


ENDPOINTS: list[EndpointSpec] = [
    EndpointSpec(method="POST", path="/api/v1/sessions", summary="创建会话",
                 request="SessionCreate", response="SessionDetail",
                 errors=["INVALID_ARGUMENT", "MODEL_NOT_FOUND"]),
    EndpointSpec(method="GET", path="/api/v1/sessions", summary="会话列表（按最近活跃排序，标题搜索，AC-004-3）",
                 response="SessionPage",
                 query=[QueryParamSpec(name="title", type="string",
                                       description="标题子串搜索定位（AC-004-3）")]),
    EndpointSpec(method="GET", path="/api/v1/sessions/{session_id}", summary="会话详情（含 run_state 刷新恢复）",
                 response="SessionDetail", errors=["SESSION_NOT_FOUND"]),
    EndpointSpec(method="PATCH", path="/api/v1/sessions/{session_id}", summary="更新会话（模型/档位热切换 AC-014-2、模式切换）",
                 request="SessionPatch", response="SessionDetail",
                 errors=["SESSION_NOT_FOUND", "INVALID_ARGUMENT", "MODEL_NOT_FOUND"]),
    EndpointSpec(method="DELETE", path="/api/v1/sessions/{session_id}", summary="删除会话（同步 kill 沙箱，AC-002-3）",
                 response=None, errors=["SESSION_NOT_FOUND"]),
    EndpointSpec(method="POST", path="/api/v1/sessions/{session_id}/messages", summary="发送用户消息（启动 run）",
                 request="MessageCreate", response="MessageSendAck",
                 errors=["SESSION_NOT_FOUND", "MESSAGE_EMPTY", "RUN_ALREADY_ACTIVE"]),
    EndpointSpec(method="GET", path="/api/v1/sessions/{session_id}/messages", summary="消息历史（游标分页；面板重建走事件流，本端点供导出/检索）",
                 response="MessagePage", errors=["SESSION_NOT_FOUND"],
                 query=[QueryParamSpec(name="cursor", type="string",
                                       description="游标分页：请求值 = 上一页响应 MessagePage.next_cursor；缺省取第一页")]),
    EndpointSpec(method="GET", path="/api/v1/sessions/{session_id}/events", summary="SSE 事件流（重放+实时；Last-Event-ID/after_seq 断线续传）",
                 response=None, sse=True, errors=["SESSION_NOT_FOUND"],
                 query=[QueryParamSpec(name="after_seq", type="int",
                                       description="断线续传水位（仅当无 Last-Event-ID 请求头时生效；头优先，见模块 docstring）")]),
    EndpointSpec(method="GET", path="/api/v1/sessions/{session_id}/costs", summary="会话成本（模型+沙箱，AC-015-3）",
                 response="SessionCost", errors=["SESSION_NOT_FOUND"]),
    EndpointSpec(method="GET", path="/api/v1/sessions/{session_id}/sandbox", summary="沙箱状态与费用估算（AC-015-4）",
                 response="SandboxStatus", errors=["SESSION_NOT_FOUND"]),
    EndpointSpec(method="POST", path="/api/v1/runs/{run_id}/stop", summary="停止运行（AC-001-2，幂等）",
                 response="StopRunAck", errors=["RUN_NOT_FOUND", "RUN_NOT_ACTIVE"]),
    EndpointSpec(method="GET", path="/api/v1/approvals", summary="审批列表（status=pending 恢复挂起卡片，AC-006-5）",
                 response="ApprovalPage",
                 query=[QueryParamSpec(name="status", type="enum",
                                       enum=["pending", "approved", "rejected", "edited",
                                             "responded", "auto_approved"],
                                       description="按状态过滤（AC-006-5 刷新恢复用 status=pending）"),
                        QueryParamSpec(name="session_id", type="string",
                                       description="按会话过滤")]),
    EndpointSpec(method="GET", path="/api/v1/approvals/{approval_id}", summary="审批详情",
                 response="ApprovalItem", errors=["APPROVAL_NOT_FOUND"]),
    EndpointSpec(method="POST", path="/api/v1/approvals/{approval_id}/decide", summary="审批决策（四决策 + resume，REQ-006）",
                 request="ApprovalDecisionRequest", response="ApprovalDecisionAck",
                 errors=["APPROVAL_NOT_FOUND", "APPROVAL_ALREADY_RESOLVED", "INVALID_ARGUMENT"]),
    EndpointSpec(method="GET", path="/api/v1/models", summary="模型列表（能力过滤的档位集合，AC-014-1/3）",
                 response="ModelList"),
    EndpointSpec(method="GET", path="/api/v1/costs/summary", summary="成本三维汇总（会话/日/模型，AC-015-2）",
                 response="CostSummary",
                 query=[QueryParamSpec(name="dimension", type="enum",
                                       enum=["session", "date", "model"], required=True,
                                       description="汇总维度（AC-015-2 三维，必填）")]),
    EndpointSpec(method="GET", path="/api/v1/diff-reviews/{review_id}", summary="diff 审查详情（逐文件摘要）",
                 response="DiffReviewDetail", errors=["DIFF_REVIEW_NOT_FOUND"]),
    EndpointSpec(method="GET", path="/api/v1/diff-reviews/{review_id}/files/{file_path}", summary="单文件完整 patch（审查面板加载；file_path 为路径参数且含 `/`，实现必须用 :path 路由转换器）",
                 response="DiffFileDetail", errors=["DIFF_REVIEW_NOT_FOUND", "NOT_FOUND"]),
    EndpointSpec(method="POST", path="/api/v1/diff-reviews/{review_id}/verdict", summary="diff 裁决（accepted 后创建基线提交，AC-007-3/4）",
                 request="DiffVerdictRequest", response="DiffReviewDetail",
                 errors=["DIFF_REVIEW_NOT_FOUND", "DIFF_ALREADY_RESOLVED", "INVALID_ARGUMENT"]),
]

REST_DTOS: dict[str, type[ContractModel]] = {
    "SessionCreate": SessionCreate,
    "SessionPatch": SessionPatch,
    "SessionDetail": SessionDetail,
    "SessionPage": SessionPage,
    "MessageCreate": MessageCreate,
    "MessageSendAck": MessageSendAck,
    "MessageItem": MessageItem,
    "MessagePage": MessagePage,
    "StopRunAck": StopRunAck,
    "ApprovalItem": ApprovalItem,
    "ApprovalPage": ApprovalPage,
    "ApprovalDecisionRequest": ApprovalDecisionRequest,
    "ApprovalDecisionAck": ApprovalDecisionAck,
    "DiffReviewDetail": DiffReviewDetail,
    "DiffFileDetail": DiffFileDetail,
    "DiffVerdictRequest": DiffVerdictRequest,
    "ModelInfo": ModelInfo,
    "ModelList": ModelList,
    "CostSummaryRow": CostSummaryRow,
    "CostSummary": CostSummary,
    "SandboxCostEstimate": SandboxCostEstimate,
    "SessionCost": SessionCost,
    "SandboxStatus": SandboxStatus,
}
