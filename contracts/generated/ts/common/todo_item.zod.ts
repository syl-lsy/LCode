import { z } from "zod";
export const TodoItemSchema = z.object({ "content": z.string(), "status": z.enum(["pending","in_progress","completed"]) }).strict().describe("todo 条目（REQ-005）。todo 面板数据源 = 最近一次 write_todos 工具调用的快照。")
