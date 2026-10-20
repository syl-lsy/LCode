// @vitest-environment jsdom
/**
 * Toast（F28；AC-003-3 / NFR-006 断线可见）：
 * reconnecting = 持续提示「正在重连」；offline = 「连接已断开」+ 可关闭；
 * 断开 → open 恢复沿短暂提示「连接已恢复」并自动消失；未断过的 open 不提示。
 */
import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import '../tests/test-utils';
import { TOAST_RECOVER_MS, Toast } from './Toast';

describe('Toast · 连接状态（AC-003-3）', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('reconnecting：显示「连接中断，正在重连…」（role=alert 可被读屏播报）', () => {
    render(<Toast state="reconnecting" />);
    const toast = screen.getByTestId('toast-conn');
    expect(toast).toHaveAttribute('role', 'alert');
    expect(toast).toHaveTextContent('连接中断，正在重连…');
  });

  it('offline：「连接已断开」+ 关闭按钮回调', async () => {
    const user = userEvent.setup();
    const onDismiss = vi.fn();
    render(<Toast state="offline" onDismiss={onDismiss} />);
    expect(screen.getByTestId('toast-conn')).toHaveTextContent('连接已断开');
    await user.click(screen.getByTestId('toast-dismiss'));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('恢复沿：reconnecting → open 提示「连接已恢复」，自动消失', () => {
    vi.useFakeTimers();
    const view = render(<Toast state="reconnecting" />);
    view.rerender(<Toast state="open" />);
    expect(screen.getByTestId('toast-conn')).toHaveTextContent('连接已恢复');
    act(() => {
      vi.advanceTimersByTime(TOAST_RECOVER_MS + 10);
    });
    expect(screen.queryByTestId('toast-conn')).not.toBeInTheDocument();
  });

  it('首次即 open（从未断过）：不提示', () => {
    render(<Toast state="open" />);
    expect(screen.queryByTestId('toast-conn')).not.toBeInTheDocument();
  });

  it('idle/connecting/closed：不渲染', () => {
    const view = render(<Toast state="idle" />);
    expect(screen.queryByTestId('toast-conn')).not.toBeInTheDocument();
    view.rerender(<Toast state="connecting" />);
    expect(screen.queryByTestId('toast-conn')).not.toBeInTheDocument();
    view.rerender(<Toast state="closed" />);
    expect(screen.queryByTestId('toast-conn')).not.toBeInTheDocument();
  });
});
