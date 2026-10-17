import { z } from "zod";
export const SessionCostSchema = z.object({ "model": z.any(), "model_cost": z.number().gte(0), "sandbox": z.any() }).strict()
