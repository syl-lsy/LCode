export type EventUnion =
  | MessageCreatedEvent
  | MessageDeltaEvent
  | MessageFinishedEvent
  | ToolCallStartedEvent
  | ToolCallFinishedEvent
  | ApprovalRequestEvent
  | ApprovalResolvedEvent
  | DiffReadyEvent
  | DiffResolvedEvent
  | SandboxOutputEvent
  | RunStatusEvent
  | BrowserActionEvent;
export type AgentId = string;
export type ParentAgentId = string | null;
export type Role = string | null;
export type SpawnedByToolCallId = string | null;
export type Content = string;
export type CreatedAt = string;
export type MessageId = string;
export type Role1 = "user";
export type RunId = string | null;
/**
 * 会话内单调递增序号；SSE id 即此值
 */
export type Seq = number;
export type SessionId = string;
export type Type = "message_created";
export type CreatedAt1 = string;
export type Delta = string;
export type Kind = "text" | "thinking";
export type MessageId1 = string;
export type Role2 = "assistant";
export type RunId1 = string | null;
/**
 * 会话内单调递增序号；SSE id 即此值
 */
export type Seq1 = number;
export type SessionId1 = string;
export type Type1 = "message_delta";
export type CreatedAt2 = string;
export type FinishReason = "stop" | "length" | "content_filter" | "error" | "cancelled";
export type MessageId2 = string;
export type Role3 = "assistant";
export type RunId2 = string | null;
/**
 * 会话内单调递增序号；SSE id 即此值
 */
export type Seq2 = number;
export type SessionId2 = string;
export type FilesReviewed = number;
export type FilesSkipped = string[];
export type Notes = string | null;
export type Category = "security" | "correctness" | "performance" | "maintainability" | "style";
export type Confidence = "high" | "medium" | "low";
export type Description = string;
export type EndLine = number | null;
export type Evidence = string | null;
export type File = string;
export type Line = number | null;
export type Severity = "critical" | "high" | "medium" | "low" | "info";
export type Suggestion = string | null;
export type Title = string;
export type Findings = Finding[];
export type SchemaVersion = "1";
export type Summary = string;
export type SchemaName = "reviewer_findings";
export type Status = "ok" | "fallback";
export type Type2 = "message_finished";
export type InputTokens = number;
export type OutputTokens = number;
export type CreatedAt3 = string;
export type RiskLevel = ("low" | "medium" | "high") | null;
export type RunId3 = string | null;
/**
 * 会话内单调递增序号；SSE id 即此值
 */
export type Seq3 = number;
export type SessionId3 = string;
export type Tool = string;
export type ToolCallId = string;
export type Type3 = "tool_call_started";
export type CreatedAt4 = string;
export type DurationMs = number;
export type Code =
  | "timeout"
  | "sandbox_unavailable"
  | "param_invalid"
  | "not_found"
  | "permission_denied"
  | "mcp_transport_error"
  | "mcp_tool_error"
  | "cancelled"
  | "internal";
export type Message = string;
export type Retryable = boolean | null;
export type ExitCode = number | null;
export type ResultPreview = string | null;
export type ResultRef = string | null;
export type RunId4 = string | null;
/**
 * 会话内单调递增序号；SSE id 即此值
 */
export type Seq4 = number;
export type SessionId4 = string;
export type Status1 = "success" | "error";
export type Todos = TodoItem[] | null;
export type Content1 = string;
export type Status2 = "pending" | "in_progress" | "completed";
export type ToolCallId1 = string;
export type Truncated = boolean;
export type Type4 = "tool_call_finished";
export type ApprovalId = string;
export type CreatedAt5 = string;
export type Reason = string | null;
export type RiskLevel1 = "medium" | "high";
export type RunId5 = string | null;
/**
 * 会话内单调递增序号；SSE id 即此值
 */
export type Seq5 = number;
export type SessionId5 = string;
export type Tool1 = string;
export type ToolCallId2 = string;
export type Type5 = "approval_request";
export type ApprovalId1 = string;
export type CreatedAt6 = string;
export type DecidedBy = string;
export type Decision = "approve" | "edit" | "reject" | "respond";
export type EditedArgs = {
  [k: string]: unknown;
} | null;
export type Feedback = string | null;
export type RunId6 = string | null;
export type Scope = "once" | "session";
/**
 * 会话内单调递增序号；SSE id 即此值
 */
export type Seq6 = number;
export type SessionId6 = string;
export type Type6 = "approval_resolved";
export type AdditionsTotal = number;
export type Commit = string;
export type Kind1 = "head" | "init" | "accepted";
export type Ref = string;
export type CreatedAt7 = string;
export type DeletionsTotal = number;
/**
 * @minItems 1
 */
export type Files = [DiffFileSummary, ...DiffFileSummary[]];
export type Additions = number;
export type Binary = boolean;
export type Change = "added" | "modified" | "deleted" | "renamed";
export type Deletions = number;
export type OldPath = string | null;
export type Path = string;
export type PatchRef = string;
export type ReviewId = string;
export type Round = number;
export type RunId7 = string | null;
/**
 * 会话内单调递增序号；SSE id 即此值
 */
export type Seq7 = number;
export type SessionId7 = string;
export type Type7 = "diff_ready";
export type CreatedAt8 = string;
export type DecidedBy1 = string;
export type Feedback1 = string | null;
export type ReviewId1 = string;
export type Round1 = number;
export type RunId8 = string | null;
/**
 * 会话内单调递增序号；SSE id 即此值
 */
export type Seq8 = number;
export type SessionId8 = string;
export type Type8 = "diff_resolved";
export type Verdict = "accepted" | "rejected";
export type Channel = "stdout" | "stderr" | "lifecycle";
export type CreatedAt9 = string;
export type Data = string;
export type ExecId = string | null;
export type Phase = ("creating" | "running" | "paused" | "resumed" | "killed" | "crashed" | "recreated") | null;
export type Provider = ("bailian" | "opensandbox") | null;
export type Reason1 = string | null;
export type RunId9 = string | null;
export type SandboxId = string;
/**
 * 会话内单调递增序号；SSE id 即此值
 */
export type Seq9 = number;
export type SessionId9 = string;
export type Type9 = "sandbox_output";
export type AfterTokens = number;
export type BeforeTokens = number;
export type CreatedAt10 = string;
export type Code1 = "model_error" | "rate_limited" | "context_window_exceeded" | "cancelled_by_user" | "internal";
export type Message1 = string;
export type Retryable1 = boolean | null;
export type Phase1 = "started" | "finished" | "stopped" | "error";
export type RunId10 = string | null;
/**
 * 会话内单调递增序号；SSE id 即此值
 */
export type Seq10 = number;
export type SessionId10 = string;
export type Subject = "run" | "compact";
export type Type10 = "run_status";
export type Action = string;
export type CreatedAt11 = string;
export type RunId11 = string | null;
export type ScreenshotRef = string | null;
/**
 * 会话内单调递增序号；SSE id 即此值
 */
export type Seq11 = number;
export type SessionId11 = string;
export type Status3 = "started" | "finished" | "error";
export type Type11 = "browser_action";
export type Url = string | null;

/**
 * 用户消息回声：落库后、run 启动前发出（多端一致 + 纯事件回放可重建，AC-008-3）。
 */
export interface MessageCreatedEvent {
  agent?: AgentRef | null;
  content: Content;
  created_at: CreatedAt;
  message_id: MessageId;
  role?: Role1;
  run_id?: RunId;
  seq: Seq;
  session_id: SessionId;
  type?: Type;
}
/**
 * 事件归属的子智能体标识；envelope.agent 为 null 表示主智能体。
 *
 * - agent_id：deepagents task() 派生的子线程标识
 * - role：general-purpose / explorer / coder / reviewer 或 P1 自定义
 * - spawned_by_tool_call_id：派生该子智能体的 task() 工具调用 id —— 前端用它把
 *   主流中的 task() 工具卡片与子智能体进度面板（message_delta 兄弟流）关联
 */
export interface AgentRef {
  agent_id: AgentId;
  parent_agent_id?: ParentAgentId;
  role?: Role;
  spawned_by_tool_call_id?: SpawnedByToolCallId;
}
/**
 * token 增量（REQ-003）。kind=thinking 对应思考折叠块（推理模型 reasoning 流）。
 */
export interface MessageDeltaEvent {
  agent?: AgentRef | null;
  created_at: CreatedAt1;
  delta: Delta;
  kind?: Kind;
  message_id: MessageId1;
  role?: Role2;
  run_id?: RunId1;
  seq: Seq1;
  session_id: SessionId1;
  type?: Type1;
}
/**
 * assistant 消息收口：finish_reason + 用量 + 可选结构化结果。
 */
export interface MessageFinishedEvent {
  agent?: AgentRef | null;
  created_at: CreatedAt2;
  finish_reason: FinishReason;
  message_id: MessageId2;
  role?: Role3;
  run_id?: RunId2;
  seq: Seq2;
  session_id: SessionId2;
  structured?: StructuredResult | null;
  type?: Type2;
  usage?: Usage | null;
}
/**
 * 结构化输出附带块（REQ-011）。status=fallback 时 data=null 且前端标注「未结构化」。
 */
export interface StructuredResult {
  data?: ReviewerFindings | null;
  schema_name?: SchemaName;
  status?: Status;
}
/**
 * reviewer 子智能体的结构化返回（汇总端机器可拼，AC-011-1）。
 */
export interface ReviewerFindings {
  coverage?: Coverage | null;
  findings?: Findings;
  schema_version?: SchemaVersion;
  summary: Summary;
}
/**
 * 审查覆盖面（扇出场景下部分失败要如实呈现，AC-009-4）。
 */
export interface Coverage {
  files_reviewed?: FilesReviewed;
  files_skipped?: FilesSkipped;
  notes?: Notes;
}
/**
 * 单条发现。line/end_line 为 1 起始行号；文件级问题（无行号）两者皆 null。
 */
export interface Finding {
  category: Category;
  confidence?: Confidence;
  description: Description;
  end_line?: EndLine;
  evidence?: Evidence;
  file: File;
  line?: Line;
  severity: Severity;
  suggestion?: Suggestion;
  title: Title;
}
/**
 * token 用量快照。口径（PRD 硬约束 9）：思考系模型的推理 token 计入 output。
 */
export interface Usage {
  input_tokens?: InputTokens;
  output_tokens?: OutputTokens;
}
/**
 * 工具调用开始（卡片出现：工具名 + 完整入参，AC-003-2）。
 */
export interface ToolCallStartedEvent {
  agent?: AgentRef | null;
  args: Args;
  created_at: CreatedAt3;
  risk_level?: RiskLevel;
  run_id?: RunId3;
  seq: Seq3;
  session_id: SessionId3;
  tool: Tool;
  tool_call_id: ToolCallId;
  type?: Type3;
}
export interface Args {
  [k: string]: unknown;
}
/**
 * 工具调用结束（卡片补全：出参摘要 + 状态，AC-003-2）。
 *
 * - error 双通道原则（调研 §4.2）：给模型仍走 ToolMessage；本事件面向 UI/落库，
 *   带 status/error{code,message,retryable}/truncated/duration_ms；
 * - exit_code：execute 类命令退出码（非 0 不代表 status=error —— deepagents
 *   口径：命令跑了就是 success，读 exit_code 判断命令失败）；
 * - todos：tool=write_todos 时携带全量 todo 快照（REQ-005 面板数据源）；
 * - result_ref：大结果被 offload 后的沙箱内落盘引用（AC-010-2 留痕）。
 */
export interface ToolCallFinishedEvent {
  agent?: AgentRef | null;
  created_at: CreatedAt4;
  duration_ms: DurationMs;
  error?: ToolErrorObj | null;
  exit_code?: ExitCode;
  result_preview?: ResultPreview;
  result_ref?: ResultRef;
  run_id?: RunId4;
  seq: Seq4;
  session_id: SessionId4;
  status: Status1;
  todos?: Todos;
  tool_call_id: ToolCallId1;
  truncated?: Truncated;
  type?: Type4;
}
/**
 * 事件侧工具错误对象（tool_call_finished / browser_action）。
 */
export interface ToolErrorObj {
  code: Code;
  message: Message;
  retryable?: Retryable;
}
/**
 * todo 条目（REQ-005）。todo 面板数据源 = 最近一次 write_todos 工具调用的快照。
 */
export interface TodoItem {
  content: Content1;
  status: Status2;
}
/**
 * 审批请求（REQ-006）：四决策 approve/edit/reject/respond 经 REST 决策端点提交。
 *
 * 不超时自动放行（AC-006-5）：本事件无过期语义；挂起态跨刷新由 approvals 表恢复。
 */
export interface ApprovalRequestEvent {
  agent?: AgentRef | null;
  approval_id: ApprovalId;
  args: Args1;
  created_at: CreatedAt5;
  reason?: Reason;
  risk_level: RiskLevel1;
  run_id?: RunId5;
  seq: Seq5;
  session_id: SessionId5;
  tool: Tool1;
  tool_call_id: ToolCallId2;
  type?: Type5;
}
export interface Args1 {
  [k: string]: unknown;
}
/**
 * 审批决策回声（决策经 POST /approvals/{id}/decide 落库后广播）。
 *
 * 用途：多标签页一致 + 纯事件回放可重建审批卡片终态（AC-008-3/AC-004-4）。
 */
export interface ApprovalResolvedEvent {
  agent?: AgentRef | null;
  approval_id: ApprovalId1;
  created_at: CreatedAt6;
  decided_by: DecidedBy;
  decision: Decision;
  edited_args?: EditedArgs;
  feedback?: Feedback;
  run_id?: RunId6;
  scope?: Scope;
  seq: Seq6;
  session_id: SessionId6;
  type?: Type6;
}
/**
 * diff 就绪（REQ-007）：摘要走事件，逐文件完整 patch 走 REST（patch_ref）。
 *
 * 新建文件须纳入 diff（git add -N 口径，AC-007-1）；totals 与 files 求和一致
 * 是诚实性强校验（F15：完成态呈现以 diff_ready 事实为准）。
 */
export interface DiffReadyEvent {
  additions_total: AdditionsTotal;
  agent?: AgentRef | null;
  baseline: BaselineRef;
  created_at: CreatedAt7;
  deletions_total: DeletionsTotal;
  files: Files;
  patch_ref: PatchRef;
  review_id: ReviewId;
  round: Round;
  run_id?: RunId7;
  seq: Seq7;
  session_id: SessionId7;
  type?: Type7;
}
/**
 * diff 对照基线引用（REQ-007 / AC-007-1/4）。
 *
 * kind 语义：head=输入仓库自身历史（不做 git 操作）；init=非 git 输入的
 * [lcode-baseline] 初始提交（round-0）；accepted=上轮已接受后创建的基线提交。
 */
export interface BaselineRef {
  commit: Commit;
  kind: Kind1;
  ref: Ref;
}
export interface DiffFileSummary {
  additions: Additions;
  binary?: Binary;
  change: Change;
  deletions: Deletions;
  old_path?: OldPath;
  path: Path;
}
/**
 * diff 裁决回声（AC-007-3）：accepted 后端创建基线提交成为下一轮对照（AC-007-4）。
 */
export interface DiffResolvedEvent {
  agent?: AgentRef | null;
  created_at: CreatedAt8;
  decided_by: DecidedBy1;
  feedback?: Feedback1;
  review_id: ReviewId1;
  round: Round1;
  run_id?: RunId8;
  seq: Seq8;
  session_id: SessionId8;
  type?: Type8;
  verdict: Verdict;
}
/**
 * 沙箱输出（REQ-002）：
 * - channel=stdout/stderr：命令输出增量（终端标签实时滚动）。exec_id 关联发起命令
 *   的工具调用（execute 类工具的 exec_id = tool_call_id；命令与退出码经
 *   tool_call_finished.exit_code 补全）；
 * - channel=lifecycle：沙箱生命周期（phase: creating/running/paused/resumed/
 *   killed/crashed/recreated）——「沙箱暂停/恢复」状态可见性的载体（PRD §9）。
 */
export interface SandboxOutputEvent {
  agent?: AgentRef | null;
  channel: Channel;
  created_at: CreatedAt9;
  data?: Data;
  exec_id?: ExecId;
  phase?: Phase;
  provider?: Provider;
  reason?: Reason1;
  run_id?: RunId9;
  sandbox_id: SandboxId;
  seq: Seq9;
  session_id: SessionId9;
  type?: Type9;
}
/**
 * 运行/压缩状态（Q5 定稿新增族）：
 *
 * - subject=run：phase started/finished/stopped/error（停止与错误路径的 UI 状态机
 *   信号，AC-001-2 / NFR-006；无法从消息族可靠推导）；
 * - subject=compact：phase started/finished（上下文压缩提示，AC-010-1），
 *   finished 时必须带 compact 前后窗口占用。
 */
export interface RunStatusEvent {
  agent?: AgentRef | null;
  compact?: CompactInfo | null;
  created_at: CreatedAt10;
  error?: RunErrorObj | null;
  phase: Phase1;
  run_id?: RunId10;
  seq: Seq10;
  session_id: SessionId10;
  subject: Subject;
  type?: Type10;
  usage_totals?: Usage | null;
}
/**
 * 上下文压缩前后窗口占用（REQ-010 / AC-010-1 界面提示）。
 */
export interface CompactInfo {
  after_tokens: AfterTokens;
  before_tokens: BeforeTokens;
}
/**
 * 事件侧运行错误对象（run_status phase=error）。
 */
export interface RunErrorObj {
  code: Code1;
  message: Message1;
  retryable?: Retryable1;
}
/**
 * 浏览器动作（REQ-102 / 场景 F）。P1 占位：P0 服务端不得产出本事件；
 * schema 先行冻结以稳定契约，前端 P0 不实现消费端。
 */
export interface BrowserActionEvent {
  action: Action;
  agent?: AgentRef | null;
  created_at: CreatedAt11;
  error?: ToolErrorObj | null;
  run_id?: RunId11;
  screenshot_ref?: ScreenshotRef;
  seq: Seq11;
  session_id: SessionId11;
  status: Status3;
  type?: Type11;
  url?: Url;
}
