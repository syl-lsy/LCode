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
