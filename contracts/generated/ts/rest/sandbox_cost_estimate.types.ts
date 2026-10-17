export type Accrued = number;
export type Currency = string;
export type RatePerHour = number;

export interface SandboxCostEstimate {
  accrued: Accrued;
  currency?: Currency;
  rate_per_hour: RatePerHour;
}
