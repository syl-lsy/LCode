export type CreatedAt = string;
export type DecidedAt = string | null;
export type Decision = ("approve" | "edit" | "reject" | "respond") | null;
export type Id = string;
export type RiskLevel = "medium" | "high";
export type Scope = "once" | "session";
export type SessionId = string;
export type Status = "pending" | "approved" | "rejected" | "edited" | "responded";
export type Tool = string;
export type Items = ApprovalItem[];
export type NextCursor = string | null;

export interface ApprovalPage {
  items: Items;
  next_cursor?: NextCursor;
}
export interface ApprovalItem {
  args: Args;
  created_at: CreatedAt;
  decided_at?: DecidedAt;
  decision?: Decision;
  id: Id;
  risk_level: RiskLevel;
  scope?: Scope;
  session_id: SessionId;
  status: Status;
  tool: Tool;
}
export interface Args {
  [k: string]: unknown;
}
