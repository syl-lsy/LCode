// @vitest-environment jsdom
/**
 * TodoPanel 单测（REQ-005 / AC-005-3；F13 状态卡改型）：
 * 空态完全不渲染、三态可区分、进度摘要、全量快照替换无幽灵条目、
 * 展开态（进程头 + Git 工具组行 + todo 列表）/ 胶囊态两形态、仅更改计数态、长列表。
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

describe('TodoPanel（REQ-005 · F13 状态卡）', () => {
  it('空快照且无更改计数：完全不渲染（AC-005-3：单步任务无 todos，不出空壳）', () => {
    render(<TodoPanel />);
    expect(screen.queryByTestId('todo-panel')).not.toBeInTheDocument();
  });

  it('非空渲染：进程头 + 进度摘要 + Git 工具组行 + 三态行（data-status 可区分）', () => {
    useTodoStore.getState().setSnapshot(FOUR, '2026-10-18T00:00:00+00:00');
    render(<TodoPanel />);

    expect(screen.getByTestId('todo-panel')).toHaveTextContent('进程');
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
    expect(rows[0]).toHaveClass('done');
  });

  it('F15 诚实性：无真实 diff_ready 时不渲染更改徽标（不伪造演示值 +58 -0）；Git 行与提交置灰照常', () => {
    useTodoStore.getState().setSnapshot(FOUR, 't1');
    render(<TodoPanel />);

    // 更改徽标族不出现（原型 +58 -0 是设计稿演示值，非产品数据）
    expect(screen.queryByTestId('todo-changes')).not.toBeInTheDocument();
    const git = screen.getByTestId('todo-git-tools');
    expect(git).not.toHaveTextContent('+58');
    expect(git).not.toHaveTextContent('-0');
    expect(git).not.toHaveTextContent('更改');
    // title 披露（对齐 App.tsx 头部 pill 口径）：diff_ready 到达后显示真实计数
    expect(git).toHaveAttribute('title', expect.stringContaining('diff_ready'));
    // Git 行其余功能照常：分支占位 + 提交置灰
    expect(git).toHaveTextContent('main');
    const commitBtn = screen.getByText('提交或推送');
    expect(commitBtn).toBeDisabled();
  });

  it('F15 诚实性：传入真实更改计数时以真实值为准（占位值不出现）', () => {
    useTodoStore.getState().setSnapshot(FOUR, 't1');
    render(<TodoPanel changes={{ additions: 17, deletions: 3 }} />);

    const git = screen.getByTestId('todo-git-tools');
    expect(git).toHaveTextContent('+17');
    expect(git).toHaveTextContent('-3');
    expect(git).not.toHaveTextContent('+58');
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

  it('可折叠：点头部收起为胶囊（展开状态；无 diff_ready 时不渲染更改徽标），再点展开', async () => {
    const user = userEvent.setup();
    useTodoStore.getState().setSnapshot(FOUR, 't1');
    render(<TodoPanel />);

    await user.click(screen.getByTestId('todo-panel-toggle'));
    expect(screen.getByTestId('todo-panel')).toHaveAttribute('data-collapsed', 'true');
    expect(screen.queryByTestId('todo-item')).not.toBeInTheDocument();
    // 胶囊态 F15 诚实性：无 diff_ready 事实 → 仅「展开状态」，更改徽标不出现（title 披露）
    expect(screen.getByText('展开状态')).toBeInTheDocument();
    expect(screen.queryByTestId('todo-changes')).not.toBeInTheDocument();
    const capsule = screen.getByTestId('todo-panel').querySelector('.capsule');
    expect(capsule).toHaveAttribute('title', expect.stringContaining('diff_ready'));

    await user.click(screen.getByTestId('todo-panel-toggle'));
    expect(screen.getByTestId('todo-panel')).toHaveAttribute('data-collapsed', 'false');
    expect(screen.getAllByTestId('todo-item')).toHaveLength(4);
  });

  it('agent 未调用 write_todos：只显示更改计数态（胶囊仅 chg-badge，无展开钮）', () => {
    render(<TodoPanel changes={{ additions: 5, deletions: 1 }} />);

    expect(screen.getByTestId('todo-panel')).toHaveAttribute('data-collapsed', 'true');
    expect(screen.getByTestId('todo-changes')).toHaveTextContent('更改 +5 -1');
    expect(screen.queryByText('展开状态')).not.toBeInTheDocument();
    expect(screen.queryByTestId('todo-panel-toggle')).not.toBeInTheDocument();
  });

  it('长列表（20 条）全部渲染在 .status-list 内', () => {
    const many: TodoItem[] = Array.from({ length: 20 }, (_, i) => ({
      content: `任务 ${i + 1}`,
      status: i < 5 ? 'completed' : i === 5 ? 'in_progress' : 'pending',
    }));
    useTodoStore.getState().setSnapshot(many, 't1');
    render(<TodoPanel />);

    expect(screen.getAllByTestId('todo-item')).toHaveLength(20);
    expect(screen.getByTestId('todo-list')).toHaveClass('status-list');
  });
});
