export type Action = string;
export type AgentId = string;
export type ParentAgentId = string | null;
export type Role = string | null;
export type SpawnedByToolCallId = string | null;
export type CreatedAt = string;
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
export type RunId = string | null;
export type ScreenshotRef = string | null;
/**
 * 会话内单调递增序号；SSE id 即此值
 */
export type Seq = number;
export type SessionId = string;
export type Status = "started" | "finished" | "error";
export type Type = "browser_action";
export type Url = string | null;

/**
 * 浏览器动作（REQ-102 / 场景 F）。P1 占位：P0 服务端不得产出本事件；
 * schema 先行冻结以稳定契约，前端 P0 不实现消费端。
 */
export interface BrowserActionEvent {
  action: Action;
  agent?: AgentRef | null;
  created_at: CreatedAt;
  error?: ToolErrorObj | null;
  run_id?: RunId;
  screenshot_ref?: ScreenshotRef;
  seq: Seq;
  session_id: SessionId;
  status: Status;
  type?: Type;
  url?: Url;
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
