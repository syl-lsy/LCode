import { z } from "zod";
export const DiffVerdictRequestSchema = z.object({ "feedback": z.union([z.string(), z.null()]).default(null), "verdict": z.enum(["accepted","rejected"]) }).strict()
