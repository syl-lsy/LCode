import { z } from "zod";
export const ApprovalPageSchema = z.object({ "items": z.array(z.any()), "next_cursor": z.union([z.string(), z.null()]).default(null) }).strict()
