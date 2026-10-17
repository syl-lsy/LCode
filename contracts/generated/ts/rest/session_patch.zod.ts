import { z } from "zod";
export const SessionPatchSchema = z.object({ "approval_mode": z.union([z.enum(["manual","auto"]), z.null()]).default(null), "model": z.union([z.string(), z.null()]).default(null), "reasoning_effort": z.union([z.enum(["off","low","medium","high"]), z.null()]).default(null), "title": z.union([z.string(), z.null()]).default(null) }).strict()
