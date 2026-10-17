export type AgentId = string;
export type ParentAgentId = string | null;
export type Role = string | null;
export type SpawnedByToolCallId = string | null;
export type Channel = "stdout" | "stderr" | "lifecycle";
export type CreatedAt = string;
export type Data = string;
export type ExecId = string | null;
export type Phase = ("creating" | "running" | "paused" | "resumed" | "killed" | "crashed" | "recreated") | null;
export type Provider = ("bailian" | "opensandbox") | null;
export type Reason = string | null;
export type RunId = string | null;
export type SandboxId = string;
/**
 * 会话内单调递增序号；SSE id 即此值
 */
export type Seq = number;
export type SessionId = string;
export type Type = "sandbox_output";

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
  created_at: CreatedAt;
  data?: Data;
  exec_id?: ExecId;
  phase?: Phase;
  provider?: Provider;
  reason?: Reason;
  run_id?: RunId;
  sandbox_id: SandboxId;
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
