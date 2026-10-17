export type DisplayName = string;
export type Id = string;
export type IsDefault = boolean;
export type Provider = string;
export type ReasoningEfforts = ("off" | "low" | "medium" | "high")[];
export type SupportsReasoning = boolean;

export interface ModelInfo {
  display_name: DisplayName;
  id: Id;
  is_default?: IsDefault;
  provider: Provider;
  reasoning_efforts: ReasoningEfforts;
  supports_reasoning: SupportsReasoning;
}
