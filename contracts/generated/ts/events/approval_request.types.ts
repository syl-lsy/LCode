export type AgentId = string;
export type ParentAgentId = string | null;
export type Role = string | null;
export type SpawnedByToolCallId = string | null;
export type ApprovalId = string;
export type CreatedAt = string;
export type Reason = string | null;
export type RiskLevel = "medium" | "high";
export type RunId = string | null;
/**
 * 会话内单调递增序号；SSE id 即此值
 */
export type Seq = number;
export type SessionId = string;
export type Tool = string;
export type ToolCallId = string;
export type Type = "approval_request";

/**
 * 审批请求（REQ-006）：四决策 approve/edit/reject/respond 经 REST 决策端点提交。
 *
 * 不超时自动放行（AC-006-5）：本事件无过期语义；挂起态跨刷新由 approvals 表恢复。
 */
export interface ApprovalRequestEvent {
  agent?: AgentRef | null;
  approval_id: ApprovalId;
  args: Args;
  created_at: CreatedAt;
  reason?: Reason;
  risk_level: RiskLevel;
  run_id?: RunId;
  seq: Seq;
  session_id: SessionId;
  tool: Tool;
  tool_call_id: ToolCallId;
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
export interface Args {
  [k: string]: unknown;
}
