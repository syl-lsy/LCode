export type Decision = "approve" | "edit" | "reject" | "respond";
export type EditedArgs = {
  [k: string]: unknown;
} | null;
export type Feedback = string | null;
export type Scope = "once" | "session";

/**
 * 四决策（REQ-006）。edit=改写参数后放行（AC-006-3）；respond=人工反馈（AC-006-4）。
 *
 * scope=session 对应原型「完全访问/始终允许」的会话级持续免审（R9）；P0 落地为
 * 会话 approval_mode 升级，权限规则持久化属 P1 权限 API。
 */
export interface ApprovalDecisionRequest {
  decision: Decision;
  edited_args?: EditedArgs;
  feedback?: Feedback;
  scope?: Scope;
}
