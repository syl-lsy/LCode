import { z } from "zod";
export const SessionPageSchema = z.object({ "items": z.array(z.any()), "total": z.number().int().gte(0) }).strict()
