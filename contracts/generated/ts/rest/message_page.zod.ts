import { z } from "zod";
export const MessagePageSchema = z.object({ "items": z.array(z.any()), "next_cursor": z.union([z.string(), z.null()]).default(null) }).strict()
