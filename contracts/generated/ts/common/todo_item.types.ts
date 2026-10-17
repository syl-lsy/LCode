export type Content = string;
export type Status = "pending" | "in_progress" | "completed";

/**
 * todo 条目（REQ-005）。todo 面板数据源 = 最近一次 write_todos 工具调用的快照。
 */
export interface TodoItem {
  content: Content;
  status: Status;
}
