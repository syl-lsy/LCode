export type Kind = "init" | "accepted";
export type Note = string | null;
export type Round = number;

/**
 * 基线提交消息的机器表达与双向转换（render/parse）。
 */
export interface BaselineCommitMessage {
  kind: Kind;
  note?: Note;
  round: Round;
}
