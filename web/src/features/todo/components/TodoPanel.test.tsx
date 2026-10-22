// @vitest-environment jsdom
/**
 * TodoPanel 单测（REQ-005 / AC-005-3；A4 §8.1）：
 * 空态完全不渲染、三态可区分、进度摘要、全量快照替换无幽灵条目、可折叠、长列表。
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';

import type { TodoItem } from '@contracts/common';

import { useTodoStore } from '../store';
import '../../../tests/test-utils';

import { TodoPanel } from './TodoPanel';

const FOUR: TodoItem[] = [
  { content: '克隆仓库', status: 'completed' },
  { content: '定位超时根因', status: 'completed' },
  { content: '修改 auth.py', status: 'in_progress' },
  { content: '跑 pytest', status: 'pending' },
];

beforeEach(() => {
  useTodoStore.getState().reset();
});

describe('TodoPanel（REQ-005）', () => {
  it('空快照完全不渲染（AC-005-3：单步任务无 todos，不出空壳）', () => {
    render(<TodoPanel />);
    expect(screen.queryByTestId('todo-panel')).not.toBeInTheDocument();
  });

  it('非空渲染：标题「计划」+ 进度摘要 + 三态行（data-status 可区分）', () => {
    useTodoStore.getState().setSnapshot(FOUR, '2026-10-18T00:00:00+00:00');
    render(<TodoPanel />);

    expect(screen.getByTestId('todo-panel')).toHaveTextContent('计划');
    expect(screen.getByTestId('todo-progress')).toHaveTextContent('2/4');
    const rows = screen.getAllByTestId('todo-item');
    expect(rows).toHaveLength(4);
    // 三态标记互不混淆（in_progress 必须有独立标记）
    expect(rows.map((row) => row.dataset.status)).toEqual([
      'completed',
      'completed',
      'in_progress',
      'pending',
    ]);
  });

  it('全量快照替换无幽灵条目：4 条 → 2 条，旧内容不残留', () => {
    useTodoStore.getState().setSnapshot(FOUR, 't1');
    const view = render(<TodoPanel />);
    expect(screen.getAllByTestId('todo-item')).toHaveLength(4);

    useTodoStore
      .getState()
      .setSnapshot(
        [
          { content: '新任务甲', status: 'in_progress' },
          { content: '新任务乙', status: 'pending' },
        ],
        't2',
      );
    view.rerender(<TodoPanel />);

    const rows = screen.getAllByTestId('todo-item');
    expect(rows).toHaveLength(2);
    expect(screen.queryByText('克隆仓库')).not.toBeInTheDocument();
    expect(screen.getByTestId('todo-progress')).toHaveTextContent('0/2');
  });

  it('可折叠：点标题收起为摘要行，再点展开', async () => {
    const user = userEvent.setup();
    useTodoStore.getState().setSnapshot(FOUR, 't1');
    render(<TodoPanel />);

    await user.click(screen.getByTestId('todo-panel-toggle'));
    expect(screen.getByTestId('todo-panel')).toHaveAttribute('data-collapsed', 'true');
    expect(screen.queryByTestId('todo-item')).not.toBeInTheDocument();
    // 收起后进度摘要仍在（一眼可见进展）
    expect(screen.getByTestId('todo-progress')).toHaveTextContent('2/4');

    await user.click(screen.getByTestId('todo-panel-toggle'));
    expect(screen.getByTestId('todo-panel')).toHaveAttribute('data-collapsed', 'false');
    expect(screen.getAllByTestId('todo-item')).toHaveLength(4);
  });

  it('长列表（20 条）全部渲染且容器可滚动（max-h + overflow）', () => {
    const many: TodoItem[] = Array.from({ length: 20 }, (_, i) => ({
      content: `任务 ${i + 1}`,
      status: i < 5 ? 'completed' : i === 5 ? 'in_progress' : 'pending',
    }));
    useTodoStore.getState().setSnapshot(many, 't1');
    render(<TodoPanel />);

    expect(screen.getAllByTestId('todo-item')).toHaveLength(20);
    expect(screen.getByTestId('todo-list')).toHaveClass('overflow-y-auto');
  });
});
