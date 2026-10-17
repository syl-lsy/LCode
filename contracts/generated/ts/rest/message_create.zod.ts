import { z } from "zod";
export const MessageCreateSchema = z.object({ "content": z.string().min(1) }).strict()
