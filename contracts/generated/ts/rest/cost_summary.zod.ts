import { z } from "zod";
export const CostSummarySchema = z.object({ "dimension": z.enum(["session","date","model"]), "rows": z.array(z.any()) }).strict()
