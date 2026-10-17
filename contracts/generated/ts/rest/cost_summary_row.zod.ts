import { z } from "zod";
export const CostSummaryRowSchema = z.object({ "cost": z.number().gte(0), "currency": z.string().default("CNY"), "group_key": z.string(), "input_tokens": z.number().int().gte(0), "model_calls": z.number().int().gte(0), "output_tokens": z.number().int().gte(0), "total_tokens": z.number().int().gte(0) }).strict()
