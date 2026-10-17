export type AgentId = string;
export type ParentAgentId = string | null;
export type Role = string | null;
export type SpawnedByToolCallId = string | null;
export type CreatedAt = string;
export type DecidedBy = string;
export type Feedback = string | null;
export type ReviewId = string;
export type Round = number;
export type RunId = string | null;
/**
 * 会话内单调递增序号；SSE id 即此值
 */
export type Seq = number;
export type SessionId = string;
export type Type = "diff_resolved";
export type Verdict = "accepted" | "rejected";

/**
 * diff 裁决回声（AC-007-3）：accepted 后端创建基线提交成为下一轮对照（AC-007-4）。
 */
export interface DiffResolvedEvent {
  agent?: AgentRef | null;
  created_at: CreatedAt;
  decided_by: DecidedBy;
  feedback?: Feedback;
  review_id: ReviewId;
  round: Round;
  run_id?: RunId;
  seq: Seq;
  session_id: SessionId;
  type?: Type;
  verdict: Verdict;
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
