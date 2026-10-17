export type ApprovalMode = ("manual" | "auto") | null;
export type Model = string | null;
export type ReasoningEffort = ("off" | "low" | "medium" | "high") | null;
export type Title = string | null;

export interface SessionPatch {
  approval_mode?: ApprovalMode;
  model?: Model;
  reasoning_effort?: ReasoningEffort;
  title?: Title;
}
