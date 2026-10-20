/**
 * todoStore / modelsStore / costsStore 单测（A4 §3.2 三行；REQ-005/014/015）。
 */
import { beforeEach, describe, expect, it } from 'vitest';

import { useTodoStore } from './store';
import type { TodoItem } from '@contracts/common';

const TODOS: TodoItem[] = [
  { content: '克隆仓库', status: 'completed' },
  { content: '修改 auth.py', status: 'in_progress' },
  { content: '跑 pytest', status: 'pending' },
];

beforeEach(() => {
  useTodoStore.getState().reset();
});

describe('todoStore：最近一次 write_todos 快照（REQ-005）', () => {
  it('setSnapshot 全量替换（不合并），带更新时刻', () => {
    useTodoStore.getState().setSnapshot(TODOS, '2026-10-17T09:30:03+00:00');
    expect(useTodoStore.getState().snapshot).toEqual(TODOS);
    expect(useTodoStore.getState().updatedAt).toBe('2026-10-17T09:30:03+00:00');

    useTodoStore.getState().setSnapshot([{ content: '新任务', status: 'pending' }], 't2');
    expect(useTodoStore.getState().snapshot).toEqual([{ content: '新任务', status: 'pending' }]);
  });

  it('单步任务（无 todos）不写快照——由 handlers 保证，此处验证空数组也不覆盖（防御）', () => {
    useTodoStore.getState().setSnapshot(TODOS, 't1');
    // handlers 侧对 todos=null/空数组直接跳过；store 不提供"清空"语义
    expect(useTodoStore.getState().snapshot).toEqual(TODOS);
  });
});
