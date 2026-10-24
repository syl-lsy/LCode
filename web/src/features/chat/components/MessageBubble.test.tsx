// @vitest-environment jsdom
/**
 * MessageBubble 单测（F1 用户消息改型 + F4 完成态操作栏）：
 * 右对齐卡 + 复制/编辑（编辑仅最新一条，点击轻提示）、长消息 clamp 切换（R9）、
 * 完成态操作栏（复制/赞/踩/分叉/钩子 + 时间戳）、error/cancelled 收口不出操作栏。
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { partKeyOf, useChatStore, type ChatMessageItem } from '../store';
import '../../../tests/test-utils';

import { MessageBubble } from './MessageBubble';

/** 从 store 取 message 条目（类型收窄；不存在即测试失败）。 */
function messageOf(id: string): ChatMessageItem {
  const item = useChatStore.getState().byId.get(id);
  if (item === undefined || item.kind !== 'message') throw new Error(`message ${id} not found`);
  return item;
}

const LONG_TEXT =
  '在 /root/obs-demo 目录创建 hello.py，打印 Hello, obs-demo，再补一个 README 说明如何运行。' +
  'README 需要包含「运行方式」和「目录用途」两节；如果目录已存在请先检查现状再写入，' +
  '避免覆盖我之前的文件；hello.py 保持无第三方依赖，python 直接可跑。';

describe('MessageBubble · F1 用户消息右对齐卡', () => {
  it('短消息：复制可用，非最新无编辑钮', () => {
    useChatStore.getState().appendUserMessage({
      id: 'u1',
      content: '帮我修一个登录超时的 bug',
      createdAt: '2026-10-18T00:00:00+00:00',
      runId: null,
    });
    render(<MessageBubble item={messageOf('u1')} isLatestUser={false} />);

    expect(screen.getByTestId('message-user')).toHaveClass('msg-user');
    expect(screen.getByTestId('message-copy')).toBeInTheDocument();
    expect(screen.queryByTestId('message-edit')).not.toBeInTheDocument();
    // 无 clamp 钮（短消息不折叠）
    expect(screen.queryByTestId('message-clamp-toggle')).not.toBeInTheDocument();
  });

  it('最新一条：编辑钮出现，点击轻提示占位（无后端支撑不伪造）', async () => {
    const user = userEvent.setup();
    useChatStore.getState().appendUserMessage({
      id: 'u-latest',
      content: '最新消息',
      createdAt: '2026-10-18T00:00:00+00:00',
      runId: null,
    });
    render(<MessageBubble item={messageOf('u-latest')} isLatestUser={true} />);

    expect(screen.getByTestId('message-edit')).toBeInTheDocument();
    await user.click(screen.getByTestId('message-edit'));
    expect(screen.getByTestId('message-edit-hint')).toHaveTextContent('编辑功能即将开放');
  });

  it('长消息：默认 2 行折叠 + 展开/收起 chip 切换（R9）', async () => {
    const user = userEvent.setup();
    useChatStore.getState().appendUserMessage({
      id: 'u-long',
      content: LONG_TEXT,
      createdAt: '2026-10-18T00:00:00+00:00',
      runId: null,
    });
    render(<MessageBubble item={messageOf('u-long')} isLatestUser={true} />);

    expect(screen.getByTestId('message-clamp-toggle')).toHaveTextContent('展开');
    await user.click(screen.getByTestId('message-clamp-toggle'));
    expect(screen.getByTestId('message-clamp-toggle')).toHaveTextContent('收起');
    await user.click(screen.getByTestId('message-clamp-toggle'));
    expect(screen.getByTestId('message-clamp-toggle')).toHaveTextContent('展开');
  });
});

describe('MessageBubble · F4 完成态操作栏', () => {
  function seedFinished(id: string, finishReason: 'stop' | 'error' | 'cancelled'): void {
    useChatStore.getState().ensureAssistantMessage({
      id,
      createdAt: '2026-10-18T11:51:00+08:00',
      runId: 'run-1',
    });
    useChatStore.getState().setPart(partKeyOf(id, 'text'), '完成。已创建 hello.py 与 README.md。');
    useChatStore.getState().finishMessage({ id, finishReason });
  }

  it('正常收口：操作栏 = 复制/赞/踩/分叉/钩子 + 时间戳；赞可切换', async () => {
    const user = userEvent.setup();
    seedFinished('a1', 'stop');
    render(<MessageBubble item={messageOf('a1')} />);

    expect(screen.getByTestId('message-foot')).toBeInTheDocument();
    expect(screen.getByTestId('message-foot-copy')).toHaveTextContent('复制');
    expect(screen.getByTestId('message-thumb-up')).toHaveTextContent('赞');
    expect(screen.getByTestId('message-thumb-down')).toHaveTextContent('踩');
    expect(screen.getByText('分叉')).toBeInTheDocument();
    expect(screen.getByText('钩子')).toBeInTheDocument();
    // 时间戳（createdAt 本地时区 HH:MM；与组件同一换算口径，避免时区假设）
    const at = new Date('2026-10-18T11:51:00+08:00');
    const pad = (n: number): string => String(n).padStart(2, '0');
    expect(screen.getByText(`${pad(at.getHours())}:${pad(at.getMinutes())}`)).toBeInTheDocument();

    // 赞：本地反馈态切换
    expect(screen.getByTestId('message-thumb-up')).toHaveAttribute('aria-pressed', 'false');
    await user.click(screen.getByTestId('message-thumb-up'));
    expect(screen.getByTestId('message-thumb-up')).toHaveAttribute('aria-pressed', 'true');
    await user.click(screen.getByTestId('message-thumb-down'));
    expect(screen.getByTestId('message-thumb-up')).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByTestId('message-thumb-down')).toHaveAttribute('aria-pressed', 'true');
  });

  it('error / cancelled 收口：如实标注，不出操作栏（不美化）', () => {
    seedFinished('a-err', 'error');
    const view = render(<MessageBubble item={messageOf('a-err')} />);
    expect(screen.getByTestId('message-error-note')).toBeInTheDocument();
    expect(screen.queryByTestId('message-foot')).not.toBeInTheDocument();

    seedFinished('a-cancel', 'cancelled');
    view.rerender(<MessageBubble item={messageOf('a-cancel')} />);
    expect(screen.getByTestId('message-cancelled-note')).toBeInTheDocument();
    expect(screen.queryByTestId('message-foot')).not.toBeInTheDocument();
  });
});
