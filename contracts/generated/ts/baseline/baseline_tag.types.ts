export type Kind = "init" | "accepted";
export type Round = number;

/**
 * 合法 tag 的机器表达：kind=init 当且仅当 round=0。
 */
export interface BaselineTag {
  kind: Kind;
  round: Round;
}
