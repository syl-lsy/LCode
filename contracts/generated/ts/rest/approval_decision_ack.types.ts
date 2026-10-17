export type CreatedAt = string;
export type DecidedAt = string | null;
export type Decision = ("approve" | "edit" | "reject" | "respond") | null;
export type Id = string;
export type Reason = string | null;
export type RiskLevel = "medium" | "high";
export type Scope = "once" | "session";
export type SessionId = string;
export type Status = "pending" | "approved" | "rejected" | "edited" | "responded" | "auto_approved";
export type Tool = string;
export type RunId = string | null;

export interface ApprovalDecisionAck {
  approval: ApprovalItem;
  run_id?: RunId;
}
/**
 * 审批行投影（GET /approvals 列表/详情；与 approvals 表逐值对应）。
 *
 * - status 含 auto_approved：AUTO 模式分类器决策的落行（decided_by='classifier'，
 *   审计面见 contracts/README「表规格补充语义」/ REQ-013 AC-013-3）；
 * - reason：命中哪条规则/分类器理由——pending 卡片恢复（AC-006-5）必须展示，
 *   故进 REST 投影；
 * - 其余决策载荷（edited_args/feedback）以 approval_resolved 事件回放为准，
 *   不重复投影（列表仅承载卡片恢复与筛选所需字段）。
 */
export interface ApprovalItem {
  args: Args;
  created_at: CreatedAt;
  decided_at?: DecidedAt;
  decision?: Decision;
  id: Id;
  reason?: Reason;
  risk_level: RiskLevel;
  scope?: Scope;
  session_id: SessionId;
  status: Status;
  tool: Tool;
}
export interface Args {
  [k: string]: unknown;
}
