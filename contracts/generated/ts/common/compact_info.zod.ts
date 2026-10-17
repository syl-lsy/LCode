import { z } from "zod";
export const CompactInfoSchema = z.object({ "after_tokens": z.number().int(), "before_tokens": z.number().int() }).strict().describe("上下文压缩前后窗口占用（REQ-010 / AC-010-1 界面提示）。")
