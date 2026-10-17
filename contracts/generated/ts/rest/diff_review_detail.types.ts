export type AdditionsTotal = number;
export type Commit = string;
export type Kind = "head" | "init" | "accepted";
export type Ref = string;
export type CreatedAt = string;
export type DecidedBy = string | null;
export type DeletionsTotal = number;
export type Feedback = string | null;
export type Additions = number;
export type Binary = boolean;
export type Change = "added" | "modified" | "deleted" | "renamed";
export type Deletions = number;
export type OldPath = string | null;
export type Path = string;
export type Files = DiffFileSummary[];
export type Id = string;
export type ResolvedAt = string | null;
export type Round = number;
export type RunId = string | null;
export type SessionId = string;
export type Verdict = "pending" | "accepted" | "rejected";

export interface DiffReviewDetail {
  additions_total: AdditionsTotal;
  baseline: BaselineRef;
  created_at: CreatedAt;
  decided_by?: DecidedBy;
  deletions_total: DeletionsTotal;
  feedback?: Feedback;
  files: Files;
  id: Id;
  resolved_at?: ResolvedAt;
  round: Round;
  run_id?: RunId;
  session_id: SessionId;
  verdict: Verdict;
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
