export type Cost = number;
export type Currency = string;
export type GroupKey = string;
export type InputTokens = number;
export type ModelCalls = number;
export type OutputTokens = number;
export type TotalTokens = number;

export interface CostSummaryRow {
  cost: Cost;
  currency?: Currency;
  group_key: GroupKey;
  input_tokens: InputTokens;
  model_calls: ModelCalls;
  output_tokens: OutputTokens;
  total_tokens: TotalTokens;
}
