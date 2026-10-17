export type AgentId = string;
export type ParentAgentId = string | null;
export type Role = string | null;
export type SpawnedByToolCallId = string | null;
export type CreatedAt = string;
export type FinishReason = "stop" | "length" | "content_filter" | "error" | "cancelled";
export type MessageId = string;
export type Role1 = "assistant";
export type RunId = string | null;
/**
 * 会话内单调递增序号；SSE id 即此值
 */
export type Seq = number;
export type SessionId = string;
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
export type Type = "message_finished";
export type InputTokens = number;
export type OutputTokens = number;

/**
 * assistant 消息收口：finish_reason + 用量 + 可选结构化结果。
 */
export interface MessageFinishedEvent {
  agent?: AgentRef | null;
  created_at: CreatedAt;
  finish_reason: FinishReason;
  message_id: MessageId;
  role?: Role1;
  run_id?: RunId;
  seq: Seq;
  session_id: SessionId;
  structured?: StructuredResult | null;
  type?: Type;
  usage?: Usage | null;
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
