// @vitest-environment jsdom
/**
 * MessageList 容器（A4 §8.2）：kind 分发（message/approval/diffcard/compact/subagent）、
 * 连续工具卡收敛为 TimerRow 总折叠组（F3：收起后卡片藏入该行）、空态、diff 卡点击开审查标签。
 * 贴底四件套的滚动几何在 useStickToBottom.test.tsx 单测（jsdom 无法真实滚动）。
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { useChatStore } from '../store';
import { useWorkbenchStore } from '@/features/workbench/store';
import '../../../tests/test-utils';

import { MessageList, groupItems } from './MessageList';

const SESSION = 'sess-1';

describe('groupItems · 连续工具卡收敛', () => {
  it('连续 toolcard 归并为一段（组 key 稳定）；其它条目不参与归并', () => {
    const items = [
      { kind: 'message', id: 'm1', role: 'user', createdAt: 't', runId: null, agent: null },
      { kind: 'toolcard', id: 't1', tool: 'execute', args: {}, status: 'running', riskLevel: null, agent: null, createdAt: 't' },
      { kind: 'toolcard', id: 't2', tool: 'read_file', args: {}, status: 'success', riskLevel: null, agent: null, createdAt: 't' },
      { kind: 'approval', id: 'a1', tool: 'execute', status: 'pending', createdAt: 't' },
      { kind: 'toolcard', id: 't3', tool: 'write_file', args: {}, status: 'success', riskLevel: null, agent: null, createdAt: 't' },
    ] as const;

    const groups = groupItems([...items]);
    expect(groups.map((g) => g.kind)).toEqual(['single', 'tools', 'single', 'tools']);
    const first = groups[1];
    expect(first?.kind === 'tools' && first.cards.map((c) => c.id)).toEqual(['t1', 't2']);
    // 归并保持顺序与引用（append-only 语义）
    expect(groups[0]?.kind === 'single' && groups[0].item.id).toBe('m1');
  });
});

describe('MessageList · 渲染分发与折叠组', () => {
  it('空流：显示「还没有消息」空态（新会话空态）', () => {
    render(<MessageList sessionId={SESSION} />);
    expect(screen.getByTestId('empty-state')).toHaveTextContent('还没有消息');
  });

  it('工具卡段渲染计时器行 + 卡片；点计时器行收起后卡片藏入该行（F3）', async () => {
    const user = userEvent.setup();
    useChatStore.getState().appendToolCard({
      id: 't1',
      tool: 'execute',
      args: { command: 'ls' },
      createdAt: '2026-10-18T00:00:00+00:00',
    });
    useChatStore.getState().appendToolCard({
      id: 't2',
      tool: 'execute',
      args: { command: 'pwd' },
      createdAt: '2026-10-18T00:00:01+00:00',
    });

    render(<MessageList sessionId={SESSION} />);
    expect(screen.getByTestId('timer-row')).toHaveTextContent('工作中');
    expect(screen.getAllByTestId('tool-card')).toHaveLength(2);

    await user.click(screen.getByTestId('timer-row'));
    expect(screen.getByTestId('tool-group-collapsed')).toBeInTheDocument();
    expect(screen.queryByTestId('tool-card')).not.toBeInTheDocument();
    // 行文案不变（R3§D：收起后行文案不变）
    expect(screen.getByTestId('timer-row')).toHaveTextContent('工作中');

    await user.click(screen.getByTestId('timer-row'));
    expect(screen.getAllByTestId('tool-card')).toHaveLength(2);
  });

  it('status 全 success 的组显示「已工作」（非运行态文案）', () => {
    useChatStore.getState().appendToolCard({
      id: 't1',
      tool: 'execute',
      args: { command: 'ls' },
      createdAt: '2026-10-18T00:00:00+00:00',
    });
    useChatStore.getState().completeToolCall({ id: 't1', status: 'success', durationMs: 10 });
    render(<MessageList sessionId={SESSION} />);
    expect(screen.getByTestId('timer-row')).toHaveTextContent('已工作');
  });

  it('compact 状态行两态渲染（started 转圈 → finished 前后 token）', () => {
    useChatStore.getState().beginCompact({ id: 'c1', at: '2026-10-18T00:00:00+00:00' });
    const view = render(<MessageList sessionId={SESSION} />);
    expect(screen.getByTestId('compact-note')).toHaveAttribute('data-phase', 'started');
    expect(view.container.textContent).toContain('正在压缩上下文');

    useChatStore.getState().finishCompact({
      id: 'c1',
      at: '2026-10-18T00:00:05+00:00',
      beforeTokens: 49970,
      afterTokens: 9836,
    });
    view.rerender(<MessageList sessionId={SESSION} />);
    const note = screen.getByTestId('compact-note');
    expect(note).toHaveAttribute('data-phase', 'finished');
    expect(note).toHaveTextContent('49,970 → 9,836 tokens');
  });

  it('审批行终态：pending 提示 + 终态文案；diff 卡点击开右栏审查标签（workbench 编排 action）', async () => {
    const user = userEvent.setup();
    useChatStore.getState().appendApprovalItem({
      id: 'ap-1',
      tool: 'execute',
      status: 'pending',
      createdAt: '2026-10-18T00:00:00+00:00',
    });
    useChatStore.getState().appendDiffCard({
      id: 'rev-1',
      round: 2,
      fileCount: 2,
      additionsTotal: 17,
      deletionsTotal: 3,
      createdAt: '2026-10-18T00:01:00+00:00',
    });

    render(<MessageList sessionId={SESSION} />);
    expect(screen.getByTestId('approval-row')).toHaveAttribute('data-status', 'pending');
    expect(screen.getByTestId('diff-card')).toHaveTextContent('改动文件（第 2 轮）');
    expect(screen.getByTestId('diff-card')).toHaveTextContent('+17');

    await user.click(screen.getByTestId('diff-card'));
    expect(useWorkbenchStore.getState().activeTabId).toBe('review:rev-1');
    expect(useWorkbenchStore.getState().collapsed).toBe(false);
  });

  it('user 消息在流内按事件顺序渲染（F1 右对齐卡）', () => {
    useChatStore.getState().appendUserMessage({
      id: 'u1',
      content: '在 /root/obs-demo 创建 hello.py',
      createdAt: '2026-10-18T00:00:00+00:00',
      runId: null,
    });
    render(<MessageList sessionId={SESSION} />);
    expect(screen.getByTestId('message-user')).toHaveTextContent('在 /root/obs-demo 创建 hello.py');
  });
});
