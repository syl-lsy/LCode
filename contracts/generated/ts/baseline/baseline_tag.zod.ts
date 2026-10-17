import { z } from "zod";
export const BaselineTagSchema = z.object({ "kind": z.enum(["init","accepted"]), "round": z.number().int().gte(0) }).strict().describe("合法 tag 的机器表达：kind=init 当且仅当 round=0。")
