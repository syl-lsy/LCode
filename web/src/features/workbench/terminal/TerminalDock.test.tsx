// @vitest-environment jsdom
/**
 * 主列底部终端 dock（F2 真形；R10 唯一例外 RD#15）：
 * dock 条（收起态）↔ ~200px 展开区两态切换；展开区渲染 TerminalView（共享
 * terminalStore）；生命周期相位点亮状态点。
 */
import { act, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { useTerminalStore } from '../store';
import '../../../tests/test-utils';

import { TerminalDock } from './TerminalDock';

describe('TerminalDock · dock 条 ↔ 展开区（F2）', () => {
  it('默认 dock 条（一行「终端」）；点击子标签展开 ~200px 终端区', async () => {
    const user = (await import('@testing-library/user-event')).default.setup();
    render(<TerminalDock />);

    const dock = screen.getByTestId('terminal-dock');
    expect(dock).toHaveTextContent('终端');
    expect(screen.queryByTestId('terminal-dock-panel')).not.toBeInTheDocument();

    await user.click(screen.getByTestId('terminal-dock-expand'));
    const panel = screen.getByTestId('terminal-dock-panel');
    expect(panel).toBeInTheDocument();
    expect(panel.style.height).toBe('200px');
    // 展开区渲染 TerminalView（共享 store 的同一份回显）
    expect(screen.getByTestId('terminal-view')).toBeInTheDocument();
    expect(screen.getByTestId('empty-state')).toHaveTextContent('终端 · 暂无输出');
  });

  it('展开态：收起钮回 dock 条；× 关闭同样收起', async () => {
    const user = (await import('@testing-library/user-event')).default.setup();
    render(<TerminalDock />);
    await user.click(screen.getByTestId('terminal-dock-expand'));
    expect(screen.getByTestId('terminal-dock-panel')).toBeInTheDocument();

    await user.click(screen.getByTestId('terminal-dock-collapse'));
    expect(screen.queryByTestId('terminal-dock-panel')).not.toBeInTheDocument();

    await user.click(screen.getByTestId('terminal-dock-expand'));
    await user.click(screen.getByTestId('terminal-dock-close'));
    expect(screen.queryByTestId('terminal-dock-panel')).not.toBeInTheDocument();
  });

  it('dock 条生命周期状态点：running 绿点 / paused 警示点（aria-label 标注相位）', () => {
    const view = render(<TerminalDock />);
    expect(screen.queryByTestId('terminal-dock-phase')).not.toBeInTheDocument();

    act(() => {
      useTerminalStore.getState().setLifecycle({
        phase: 'running',
        at: '2026-10-18T00:00:00+00:00',
        provider: 'bailian',
        reason: null,
      });
    });
    expect(screen.getByTestId('terminal-dock-phase')).toHaveAttribute('aria-label', '沙箱 running');

    act(() => {
      useTerminalStore.getState().setLifecycle({
        phase: 'paused',
        at: '2026-10-18T00:01:00+00:00',
        provider: 'bailian',
        reason: '空闲超时',
      });
    });
    expect(screen.getByTestId('terminal-dock-phase')).toHaveAttribute('aria-label', '沙箱 paused');
    view.unmount();
  });

  it('有输出时 dock 条显示展开箭头（▴ 提示可展开）', () => {
    const view = render(<TerminalDock />);
    act(() => {
      useTerminalStore.getState().appendLines('call-x', ['line']);
    });
    expect(screen.getByTestId('terminal-dock')).toHaveTextContent('▴');
    view.unmount();
  });
});
