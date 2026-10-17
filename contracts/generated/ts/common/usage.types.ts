export type InputTokens = number;
export type OutputTokens = number;

/**
 * token 用量快照。口径（PRD 硬约束 9）：思考系模型的推理 token 计入 output。
 */
export interface Usage {
  input_tokens?: InputTokens;
  output_tokens?: OutputTokens;
}
