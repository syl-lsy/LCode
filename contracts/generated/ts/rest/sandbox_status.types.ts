export type Accrued = number;
export type Currency = string;
export type RatePerHour = number;
export type PausedSince = string | null;
/**
 * 当前状态投影（查询面）。事件侧 SandboxOutputEvent.phase 的迁移动作在此折叠：resumed/recreated → running（动作不是稳态）；none = 尚无沙箱实例。两枚举差异为有意设计——事件记迁移（含动作），状态只表达稳态
 */
export type Phase = "none" | "creating" | "running" | "paused" | "killed" | "crashed";
export type Provider = "bailian" | "opensandbox";
export type RunningSince = string | null;
export type SandboxId = string | null;

export interface SandboxStatus {
  cost_estimate?: SandboxCostEstimate | null;
  paused_since?: PausedSince;
  phase: Phase;
  provider: Provider;
  running_since?: RunningSince;
  sandbox_id?: SandboxId;
}
export interface SandboxCostEstimate {
  accrued: Accrued;
  currency?: Currency;
  rate_per_hour: RatePerHour;
}
