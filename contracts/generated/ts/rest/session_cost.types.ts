export type InputTokens = number;
export type OutputTokens = number;
export type ModelCost = number;
export type Accrued = number;
export type Currency = string;
export type RatePerHour = number;

export interface SessionCost {
  model: Usage;
  model_cost: ModelCost;
  sandbox: SandboxCostEstimate;
}
/**
 * token 用量快照。口径（PRD 硬约束 9）：思考系模型的推理 token 计入 output。
 */
export interface Usage {
  input_tokens?: InputTokens;
  output_tokens?: OutputTokens;
}
export interface SandboxCostEstimate {
  accrued: Accrued;
  currency?: Currency;
  rate_per_hour: RatePerHour;
}
