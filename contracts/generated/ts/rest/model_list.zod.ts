import { z } from "zod";
export const ModelListSchema = z.object({ "default_model": z.string(), "models": z.array(z.object({ "display_name": z.string(), "id": z.string(), "is_default": z.boolean().default(false), "provider": z.string(), "reasoning_efforts": z.array(z.enum(["off","low","medium","high"])), "supports_reasoning": z.boolean() }).strict()) }).strict()
