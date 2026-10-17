import { z } from "zod";
export const ModelListSchema = z.object({ "default_model": z.string(), "models": z.array(z.any()) }).strict()
