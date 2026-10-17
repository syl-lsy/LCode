import { z } from "zod";
export const SandboxCostEstimateSchema = z.object({ "accrued": z.number(), "currency": z.string().default("CNY"), "rate_per_hour": z.number() }).strict()
