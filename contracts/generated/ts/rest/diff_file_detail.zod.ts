import { z } from "zod";
export const DiffFileDetailSchema = z.object({ "additions": z.number().int().gte(0), "binary": z.boolean().default(false), "blob_ref": z.union([z.string(), z.null()]).default(null), "change": z.enum(["added","modified","deleted","renamed"]), "deletions": z.number().int().gte(0), "patch": z.union([z.string(), z.null()]).default(null), "path": z.string() }).strict()
