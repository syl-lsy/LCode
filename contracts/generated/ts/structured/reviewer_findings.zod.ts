import { z } from "zod";
export const ReviewerFindingsSchema = z.object({ "coverage": z.union([z.any(), z.null()]).default(null), "findings": z.array(z.any()).optional(), "schema_version": z.literal("1").default("1"), "summary": z.string() }).strict().describe("reviewer 子智能体的结构化返回（汇总端机器可拼，AC-011-1）。")
