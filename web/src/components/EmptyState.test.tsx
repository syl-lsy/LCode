// @vitest-environment jsdom
/**
 * EmptyState 空态基件（B6）：默认形态（无 head，M2 以来消息流/会话列表消费方
 * 零变化）+ frame 变体（head → 原型 .frame/.frame-head/.frame-body 类，B6 落位）。
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import '../tests/test-utils';

import { EmptyState } from './EmptyState';

describe('EmptyState · 空态基件', () => {
  it('默认形态：图标 + 标题 + 描述 + 动作（tailwind 形态保持不变）', () => {
    render(
      <EmptyState
        title="还没有消息"
        description="发送第一条消息开始会话。"
        action={<button type="button">去发送</button>}
      />,
    );
    expect(screen.getByTestId('empty-state')).toHaveTextContent('还没有消息');
    expect(screen.getByTestId('empty-state')).toHaveTextContent('发送第一条消息开始会话。');
    expect(screen.getByRole('button', { name: '去发送' })).toBeInTheDocument();
  });

  it('frame 变体（head 传入）：整体包 .frame 容器，头部渲染 frame-head，本体用原型类', () => {
    render(
      <EmptyState
        testId="archive-empty"
        icon={<span>ic</span>}
        title="暂无归档任务"
        description="在侧边栏进入归档模式后，这里列出已归档任务。"
        head={
          <>
            <span className="t">归档</span>
            <button type="button">关闭</button>
          </>
        }
      />,
    );
    const frame = screen.getByTestId('archive-empty');
    expect(frame.className).toContain('frame');
    expect(frame.querySelector('.frame-head')).not.toBeNull();
    expect(frame.querySelector('.frame-body')).not.toBeNull();
    expect(frame.querySelector('.empty-ic')).not.toBeNull();
    expect(frame.querySelector('.et')).toHaveTextContent('暂无归档任务');
    expect(frame.querySelector('.ed')).toHaveTextContent('在侧边栏进入归档模式后，这里列出已归档任务。');
    expect(frame.querySelector('.frame-head')).toHaveTextContent('归档');
  });
});
