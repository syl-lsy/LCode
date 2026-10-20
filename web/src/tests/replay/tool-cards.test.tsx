// @vitest-environment jsdom
/**
 * 回放 · 工具卡片两态（AC-003-2；test-plan §2.3）：
 * fixtures 回放 → started 卡出现（running 态）→ finished 补全（成功/失败、exit_code、
 * todos 快照）；配对按 tool_call_id（fixtures 的 started/finished 是不同调用，
 * 回放序列须自配）；失败卡片如实呈现不美化（AC-004-4 前置）。
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import messageCreated from '@contracts-fixtures/events/valid/message_created.json';
import toolCallFinishedError from '@contracts-fixtures/events/valid/tool_call_finished_error.json';
import toolCallFinishedTodos from '@contracts-fixtures/events/valid/tool_call_finished.json';
import toolCallStarted from '@contracts-fixtures/events/valid/tool_call_started.json';

import { useChatStore } from '@/features/chat/store';
import { useTodoStore } from '@/features/todo/store';
import { MessageList } from '@/features/chat/components/MessageList';

import { eventBase, feedFixtures } from './helpers';
import '../../tests/test-utils';

const SESSION = '0192f0a1-1111-4a1a-9c2d-000000000001';

describe('replay/tool-cards：两态卡片与 todos 快照', () => {
  it('started → running 态；finished → 成功态补全（耗时/出参），展开可见详情', async () => {
    const user = userEvent.setup();
    feedFixtures([
      messageCreated,
      toolCallStarted,
      eventBase(111, 'tool_call_finished', {
        tool_call_id: toolCallStarted.tool_call_id,
        status: 'success',
        duration_ms: 1200,
        result_preview: '4 passed in 1.2s',
      }),
    ]);

    render(<MessageList sessionId={SESSION} />);
    const card = screen.getByTestId('tool-card');
    expect(card).toHaveAttribute('data-status', 'success');
    expect(screen.getByText('pytest -q tests/')).toBeInTheDocument();

    await user.click(screen.getByTestId('tool-card-toggle'));
    expect(screen.getByTestId('tool-card-detail')).toHaveTextContent('4 passed in 1.2s');
    expect(screen.getByTestId('tool-card-detail')).toHaveTextContent('耗时 1200 ms');
  });

  it('started 单独到达 = running 态（spinner + 正在终端）', () => {
    feedFixtures([toolCallStarted]);
    render(<MessageList sessionId={SESSION} />);
    expect(screen.getByTestId('tool-card')).toHaveAttribute('data-status', 'running');
    expect(screen.getByText('正在终端')).toBeInTheDocument();
  });

  it('失败卡片如实呈现（不美化）：error code + message 原样可见（AC-004-4）', async () => {
    const user = userEvent.setup();
    // error fixture 的 tool_call_id 是独立调用：补齐它的 started
    feedFixtures([
      eventBase(120, 'tool_call_started', {
        tool_call_id: toolCallFinishedError.tool_call_id,
        tool: 'tavily_search',
        args: { query: 'lcode' },
      }),
      toolCallFinishedError,
    ]);
    render(<MessageList sessionId={SESSION} />);
    expect(screen.getByTestId('tool-card')).toHaveAttribute('data-status', 'error');
    expect(screen.getByTestId('tool-error-code')).toHaveTextContent('timeout');
    // 展开后完整错误文案原样保留（不美化不吞词）
    await user.click(screen.getByTestId('tool-card-toggle'));
    expect(screen.getByTestId('tool-error-detail')).toHaveTextContent(
      'tavily_search 30s 超时；操作可能仍在服务端运行，重试可能重复执行',
    );
  });

  it('todos 快照写 todoStore（REQ-005 数据源）；未知卡片的 finished 不凭空造条目', () => {
    feedFixtures([toolCallFinishedTodos]);
    expect(useTodoStore.getState().snapshot).toHaveLength(4);
    expect(useTodoStore.getState().snapshot[0]).toEqual({ content: '克隆仓库', status: 'completed' });
    // finished 的卡片不在流中（缺 started）→ 不造条目（store 写者纪律）
    expect(useChatStore.getState().items).toEqual([]);
  });

  it('exit_code 非 0 不等于 error：status=success 且出现 exit 徽标（契约口径）', () => {
    feedFixtures([
      toolCallStarted,
      eventBase(111, 'tool_call_finished', {
        tool_call_id: toolCallStarted.tool_call_id,
        status: 'success',
        duration_ms: 500,
        exit_code: 2,
        result_preview: 'command ran',
      }),
    ]);
    render(<MessageList sessionId={SESSION} />);
    expect(screen.getByTestId('tool-card')).toHaveAttribute('data-status', 'success');
    expect(screen.getByTestId('tool-exit-code')).toHaveTextContent('exit 2');
  });
});
