export type Dimension = "session" | "date" | "model";
export type Cost = number;
export type Currency = string;
export type GroupKey = string;
export type InputTokens = number;
export type ModelCalls = number;
export type OutputTokens = number;
export type TotalTokens = number;
export type Rows = CostSummaryRow[];

export interface CostSummary {
  dimension: Dimension;
  rows: Rows;
}
export interface CostSummaryRow {
  cost: Cost;
  currency?: Currency;
  group_key: GroupKey;
  input_tokens: InputTokens;
  model_calls: ModelCalls;
  output_tokens: OutputTokens;
  total_tokens: TotalTokens;
}
