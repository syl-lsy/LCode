export type ApprovalMode = "manual" | "auto";
export type CreatedAt = string;
export type Id = string;
export type LastActiveAt = string;
export type Model = string;
export type ReasoningEffort = "off" | "low" | "medium" | "high";
export type RunState = "idle" | "running" | "waiting_approval";
export type SandboxProvider = "bailian" | "opensandbox";
export type Status = "active" | "archived";
export type Title = string;
export type UpdatedAt = string;

export interface SessionDetail {
  approval_mode: ApprovalMode;
  created_at: CreatedAt;
  id: Id;
  last_active_at: LastActiveAt;
  model: Model;
  reasoning_effort: ReasoningEffort;
  run_state: RunState;
  sandbox_provider: SandboxProvider;
  status: Status;
  title: Title;
  updated_at: UpdatedAt;
}
