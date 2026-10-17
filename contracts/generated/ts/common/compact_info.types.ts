export type AfterTokens = number;
export type BeforeTokens = number;

/**
 * 上下文压缩前后窗口占用（REQ-010 / AC-010-1 界面提示）。
 */
export interface CompactInfo {
  after_tokens: AfterTokens;
  before_tokens: BeforeTokens;
}
