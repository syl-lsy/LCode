export type AgentId = string;
export type ParentAgentId = string | null;
export type Role = string | null;
export type SpawnedByToolCallId = string | null;
export type ApprovalId = string;
export type CreatedAt = string;
export type DecidedBy = string;
export type Decision = "approve" | "edit" | "reject" | "respond";
export type EditedArgs = {
  [k: string]: unknown;
} | null;
export type Feedback = string | null;
export type RunId = string | null;
export type Scope = "once" | "session";
/**
 * 会话内单调递增序号；SSE id 即此值
 */
export type Seq = number;
export type SessionId = string;
export type Type = "approval_resolved";

/**
 * 审批决策回声（决策经 POST /approvals/{id}/decide 落库后广播）。
 *
 * 用途：多标签页一致 + 纯事件回放可重建审批卡片终态（AC-008-3/AC-004-4）。
 */
export interface ApprovalResolvedEvent {
  agent?: AgentRef | null;
  approval_id: ApprovalId;
  created_at: CreatedAt;
  decided_by: DecidedBy;
  decision: Decision;
  edited_args?: EditedArgs;
  feedback?: Feedback;
  run_id?: RunId;
  scope?: Scope;
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
