import { z } from "zod";
export const UsageSchema = z.object({ "input_tokens": z.number().int().default(0), "output_tokens": z.number().int().default(0) }).strict().describe("token 用量快照。口径（PRD 硬约束 9）：思考系模型的推理 token 计入 output。")
