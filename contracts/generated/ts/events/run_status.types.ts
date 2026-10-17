export type AgentId = string;
export type ParentAgentId = string | null;
export type Role = string | null;
export type SpawnedByToolCallId = string | null;
export type AfterTokens = number;
export type BeforeTokens = number;
export type CreatedAt = string;
export type Code = "model_error" | "rate_limited" | "context_window_exceeded" | "cancelled_by_user" | "internal";
export type Message = string;
export type Retryable = boolean | null;
export type Phase = "started" | "finished" | "stopped" | "error";
export type RunId = string | null;
/**
 * 会话内单调递增序号；SSE id 即此值
 */
export type Seq = number;
export type SessionId = string;
export type Subject = "run" | "compact";
export type Type = "run_status";
export type InputTokens = number;
export type OutputTokens = number;

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
  created_at: CreatedAt;
  error?: RunErrorObj | null;
  phase: Phase;
  run_id?: RunId;
  seq: Seq;
  session_id: SessionId;
  subject: Subject;
  type?: Type;
  usage_totals?: Usage | null;
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
  code: Code;
  message: Message;
  retryable?: Retryable;
}
/**
 * token 用量快照。口径（PRD 硬约束 9）：思考系模型的推理 token 计入 output。
 */
export interface Usage {
  input_tokens?: InputTokens;
  output_tokens?: OutputTokens;
}
