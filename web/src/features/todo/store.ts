import { create } from 'zustand';

import type { TodoItem } from '@contracts/common';

/**
 * todoStore：最近一次 write_todos 快照（A4 §3.2 todoStore 行 / §5.3）。
 *
 * 契约约束（REQ-005）：todo 面板不另立事件——数据源 = tool_call_finished.todos
 * （全量快照，非增量）；单步任务后端不产出 todos，故快照为 null 时应保持空态而非
 * 造一条占位（AC-005-3）。
 */
export interface TodoStore {
  snapshot: TodoItem[];
  updatedAt: string | null;
  setSnapshot: (todos: readonly TodoItem[], at: string) => void;
  reset: () => void;
}

interface TodoData {
  snapshot: TodoItem[];
  updatedAt: string | null;
}

function createTodoSlice(): TodoData {
  return { snapshot: [], updatedAt: null };
}

export const useTodoStore = create<TodoStore>((set) => ({
  ...createTodoSlice(),
  setSnapshot: (todos, at) => set({ snapshot: [...todos], updatedAt: at }),
  reset: () => set(createTodoSlice()),
}));
