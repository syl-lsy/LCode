export type AgentId = string;
export type ParentAgentId = string | null;
export type Role = string | null;
export type SpawnedByToolCallId = string | null;
export type CreatedAt = string;
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
export type RunId = string | null;
/**
 * 会话内单调递增序号；SSE id 即此值
 */
export type Seq = number;
export type SessionId = string;
export type Status = "success" | "error";
export type Todos = TodoItem[] | null;
export type Content = string;
export type Status1 = "pending" | "in_progress" | "completed";
export type ToolCallId = string;
export type Truncated = boolean;
export type Type = "tool_call_finished";

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
  created_at: CreatedAt;
  duration_ms: DurationMs;
  error?: ToolErrorObj | null;
  exit_code?: ExitCode;
  result_preview?: ResultPreview;
  result_ref?: ResultRef;
  run_id?: RunId;
  seq: Seq;
  session_id: SessionId;
  status: Status;
  todos?: Todos;
  tool_call_id: ToolCallId;
  truncated?: Truncated;
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
  content: Content;
  status: Status1;
}
