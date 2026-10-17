import { z } from "zod";
export const BaselineCommitMessageSchema = z.object({ "kind": z.enum(["init","accepted"]), "note": z.union([z.string(), z.null()]).default(null), "round": z.number().int().gte(0) }).strict().describe("基线提交消息的机器表达与双向转换（render/parse）。")
