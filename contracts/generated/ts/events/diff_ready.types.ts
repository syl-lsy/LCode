export type AdditionsTotal = number;
export type AgentId = string;
export type ParentAgentId = string | null;
export type Role = string | null;
export type SpawnedByToolCallId = string | null;
export type Commit = string;
export type Kind = "head" | "init" | "accepted";
export type Ref = string;
export type CreatedAt = string;
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
export type RunId = string | null;
/**
 * 会话内单调递增序号；SSE id 即此值
 */
export type Seq = number;
export type SessionId = string;
export type Type = "diff_ready";

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
  created_at: CreatedAt;
  deletions_total: DeletionsTotal;
  files: Files;
  patch_ref: PatchRef;
  review_id: ReviewId;
  round: Round;
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
 * diff 对照基线引用（REQ-007 / AC-007-1/4）。
 *
 * kind 语义：head=输入仓库自身历史（不做 git 操作）；init=非 git 输入的
 * [lcode-baseline] 初始提交（round-0）；accepted=上轮已接受后创建的基线提交。
 */
export interface BaselineRef {
  commit: Commit;
  kind: Kind;
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
