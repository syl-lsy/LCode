import { useState } from 'react';

import type { TodoItem } from '@contracts/common';

import { useTodoStore } from '../store';

/**
 * todo 计划面板（REQ-005 / AC-005-3；A4 §8.1，视觉对齐 zcode-ui 12-todo-panel.png）。
 *
 * - 位置由挂载方决定（MessageList：消息流区域右上浮动卡，非独立右栏 tab）；
 * - 非空即显、空态完全不渲染（单步任务后端不产出 todos，快照为空不造空壳）；
 * - 数据源 = todoStore.snapshot（最近一次 write_todos 的**全量快照**，直接整体替换
 *   渲染，不做增量拼合 → 无幽灵条目）；
 * - 三态可区分：completed ✓ 删除线 / in_progress → 加粗 + 脉冲点 / pending ○ 弱化；
 * - 长列表内部滚动（max-h + overflow），不撑爆消息区；可折叠为摘要行（进度保留）。
 */

function TodoRow({ todo }: { todo: TodoItem }) {
  return (
    <li
      data-testid="todo-item"
      data-status={todo.status}
      className="flex items-start gap-2 py-[3px] text-[length:var(--text-ui-xs)] leading-snug"
    >
      {todo.status === 'completed' && (
        <span aria-hidden="true" className="flex-none text-success">
          ✓
        </span>
      )}
      {todo.status === 'in_progress' && (
        <span aria-hidden="true" className="pulse-dot flex-none text-foreground">
          →
        </span>
      )}
      {todo.status === 'pending' && (
        <span aria-hidden="true" className="flex-none text-foreground-subtlest">
          ○
        </span>
      )}
      <span
        className={
          todo.status === 'completed'
            ? 'text-foreground-subtlest line-through'
            : todo.status === 'in_progress'
              ? 'font-medium text-foreground'
              : 'text-foreground-subtle'
        }
      >
        {todo.content}
      </span>
    </li>
  );
}

export function TodoPanel() {
  const snapshot = useTodoStore((s) => s.snapshot);
  const [collapsed, setCollapsed] = useState(false);

  if (snapshot.length === 0) return null; // AC-005-3：空态零占位

  const completed = snapshot.filter((todo) => todo.status === 'completed').length;

  return (
    <section
      data-testid="todo-panel"
      data-collapsed={collapsed}
      aria-label="任务计划"
      className="w-64 overflow-hidden rounded-lg border border-border bg-popover shadow-md"
    >
      <button
        type="button"
        data-testid="todo-panel-toggle"
        aria-expanded={!collapsed}
        className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-surface"
        onClick={() => setCollapsed((v) => !v)}
      >
        <span className="text-[length:var(--text-ui-xs)] font-semibold text-foreground">计划</span>
        <span data-testid="todo-progress" className="badge flex-none">
          {completed}/{snapshot.length}
        </span>
        <span aria-hidden="true" className="ml-auto flex-none text-foreground-subtlest">
          {collapsed ? '▸' : '▾'}
        </span>
      </button>
      {!collapsed && (
        <ul
          data-testid="todo-list"
          className="scroll-thin max-h-56 overflow-y-auto border-t border-border px-3 py-1.5"
        >
          {snapshot.map((todo, index) => (
            <TodoRow key={`${index}:${todo.content}`} todo={todo} />
          ))}
        </ul>
      )}
    </section>
  );
}
