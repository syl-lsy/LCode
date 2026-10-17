export type Accrued = number;
export type Currency = string;
export type RatePerHour = number;
export type PausedSince = string | null;
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
