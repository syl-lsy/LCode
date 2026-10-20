// @vitest-environment jsdom
/**
 * StreamText / ThinkingBlock（AC-001-2 停止保留内容；AC-003-1 打字机落 store 后的渲染）：
 * - 正文来自 part（flush 后）；停止（run 终态 + finish_reason=cancelled）不清空已收内容；
 * - 生成中空文本显示占位；思考块独立通道、默认折叠。
 */
import { act, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { partKeyOf, useChatStore } from '../store';
import '../../../tests/test-utils';

import { StreamText } from './StreamText';
import { ThinkingBlock } from './ThinkingBlock';

const MSG = 'msg-stream-1';

function seedAssistant(parts: { text?: string; thinking?: string } = {}): void {
  useChatStore.getState().ensureAssistantMessage({ id: MSG, createdAt: 't0', runId: 'r1' });
  if (parts.text !== undefined) useChatStore.getState().setPart(partKeyOf(MSG, 'text'), parts.text);
  if (parts.thinking !== undefined) {
    useChatStore.getState().setPart(partKeyOf(MSG, 'thinking'), parts.thinking);
  }
}

describe('StreamText · 流式正文与停止保留（AC-001-2）', () => {
  it('生成中空文本：显示「正在生成…」占位而非空白', () => {
    seedAssistant();
    render(<StreamText messageId={MSG} streaming />);
    expect(screen.getByTestId('stream-loading')).toHaveTextContent('正在生成…');
  });

  it('已收文本渲染为 markdown；追加（打字机 drain）后同步更新', async () => {
    seedAssistant({ text: '第一段' });
    const view = render(<StreamText messageId={MSG} streaming />);
    expect(view.container.textContent).toContain('第一段');

    act(() => {
      useChatStore.getState().appendPart(partKeyOf(MSG, 'text'), '，第二段');
    });
    // Markdown 70ms 限频：新文本越窗后提交（限频只延迟不改变结果）
    await waitFor(() => {
      expect(view.container.textContent).toContain('第一段，第二段');
    });
  });

  it('停止保留：run_status(stopped) + finish_reason=cancelled 后已收内容不丢（AC-001-2）', () => {
    seedAssistant({ text: '已经生成到一半的内容' });
    const view = render(<StreamText messageId={MSG} streaming={false} />);

    // 服务端停止 → run 终态 + 消息收口（cancelled）
    useChatStore.getState().applyRunStatus({
      subject: 'run',
      phase: 'stopped',
      runId: 'r1',
      createdAt: 't1',
    });
    useChatStore.getState().finishMessage({ id: MSG, finishReason: 'cancelled', structured: null });

    view.rerender(<StreamText messageId={MSG} streaming={false} />);
    expect(view.container.textContent).toContain('已经生成到一半的内容');
    expect(useChatStore.getState().byId.get(MSG)).toMatchObject({ finishReason: 'cancelled' });
  });

  it('无 part 且非生成中：渲染空（不造占位）', () => {
    seedAssistant();
    const view = render(<StreamText messageId={MSG} streaming={false} />);
    expect(view.container.textContent).toBe('');
  });
});

describe('ThinkingBlock · 思考通道（message_delta kind=thinking）', () => {
  it('有思考文本 → 折叠块（summary 显示字数），展开可见正文', () => {
    seedAssistant({ thinking: '正在思考：先看目录现状。' });
    render(<ThinkingBlock messageId={MSG} />);
    const block = screen.getByTestId('thinking-block');
    expect(block).toHaveTextContent('思考 · 12 字');
    expect(block.querySelector('details, summary')).toBeTruthy();
    expect(block).toHaveTextContent('正在思考：先看目录现状。');
  });

  it('无思考文本 → 不渲染（不占位）', () => {
    seedAssistant({ text: '只有正文' });
    render(<ThinkingBlock messageId={MSG} />);
    expect(screen.queryByTestId('thinking-block')).not.toBeInTheDocument();
  });
});
