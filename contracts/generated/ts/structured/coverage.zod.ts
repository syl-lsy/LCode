import { z } from "zod";
export const CoverageSchema = z.object({ "files_reviewed": z.number().int().gte(0).default(0), "files_skipped": z.array(z.string()).optional(), "notes": z.union([z.string(), z.null()]).default(null) }).strict().describe("审查覆盖面（扇出场景下部分失败要如实呈现，AC-009-4）。")
