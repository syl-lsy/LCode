// @vitest-environment jsdom
/**
 * 主列底部终端 dock（F2 态 A 真形；R10 唯一例外 RD#15）：
 * 三态 hidden ↔ bar（dock 条）↔ panel（~200px 展开区）；Ctrl/Cmd+J 全局键位；
 * 展开区渲染 TerminalView（共享 terminalStore）+ zsh 徽标 / workspace 占位 /
 * 新建占位 / × 全收 + 只读输入行占位；生命周期相位点亮状态点。
 */
import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { useTerminalStore, useWorkbenchStore } from '../store';
import '../../../tests/test-utils';

import { TerminalDock } from './TerminalDock';

describe('TerminalDock · 三态 hidden ↔ bar ↔ panel（F2）', () => {
  it('默认 dock 条（一行「终端」）；点击子标签展开 ~200px 终端区', async () => {
    const user = userEvent.setup();
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

  it('展开区原型件齐备：zsh shell 徽标 / workspace 占位子标签 / 新建占位 / 只读输入行', async () => {
    const user = userEvent.setup();
    render(<TerminalDock />);
    await user.click(screen.getByTestId('terminal-dock-expand'));

    expect(screen.getByTitle('shell 类型：zsh')).toHaveTextContent('zsh');
    expect(screen.getByTitle('workspace 终端')).toHaveTextContent('workspace');
    // P1 多会话占位钮（aria-disabled，不伪造行为）
    expect(screen.getByTitle('新建终端（P1 多会话占位）')).toHaveAttribute('aria-disabled', 'true');
    // P0 只读回显：输入行 readOnly 不可输入
    const input = screen.getByLabelText('Terminal input');
    expect(input).toHaveAttribute('readonly');
    expect(input).toHaveAttribute('title', 'P0 只读回显，交互输入归 P1');
  });

  it('展开态：收起钮回 dock 条（dock 仍在）；× 关闭全收（dock 消失）', async () => {
    const user = userEvent.setup();
    render(<TerminalDock />);
    await user.click(screen.getByTestId('terminal-dock-expand'));
    expect(screen.getByTestId('terminal-dock-panel')).toBeInTheDocument();

    await user.click(screen.getByTestId('terminal-dock-collapse'));
    expect(screen.queryByTestId('terminal-dock-panel')).not.toBeInTheDocument();
    expect(screen.getByTestId('terminal-dock')).toBeInTheDocument();

    await user.click(screen.getByTestId('terminal-dock-expand'));
    await user.click(screen.getByTestId('terminal-dock-close'));
    // × 关闭 = 整个 dock 全收（hidden 态不渲染）
    expect(screen.queryByTestId('terminal-dock')).not.toBeInTheDocument();
  });

  it('Ctrl/Cmd+J 键位：hidden ↔ bar 两态往返；panel 态再按 = 全收', async () => {
    const user = userEvent.setup();
    render(<TerminalDock />);

    // 默认 bar → Ctrl+J 全收
    await user.keyboard('{Control>}j{/Control}');
    expect(screen.queryByTestId('terminal-dock')).not.toBeInTheDocument();
    expect(useWorkbenchStore.getState().terminalDock).toBe('hidden');
    // 再按 → 回 dock 条
    await user.keyboard('{Control>}j{/Control}');
    expect(screen.getByTestId('terminal-dock')).toBeInTheDocument();
    expect(useWorkbenchStore.getState().terminalDock).toBe('bar');

    // panel 态下再按 Ctrl+J = 全收（对齐原型 dock.hidden 判定口径）
    await user.click(screen.getByTestId('terminal-dock-expand'));
    await user.keyboard('{Meta>}j{/Meta}');
    expect(screen.queryByTestId('terminal-dock')).not.toBeInTheDocument();
  });

  it('store.setTerminalDock 直控：hidden 态不渲染（父级/键位共用同一状态源）', () => {
    const view = render(<TerminalDock />);
    act(() => {
      useWorkbenchStore.getState().setTerminalDock('hidden');
    });
    expect(screen.queryByTestId('terminal-dock')).not.toBeInTheDocument();
    act(() => {
      useWorkbenchStore.getState().setTerminalDock('bar');
    });
    expect(screen.getByTestId('terminal-dock')).toBeInTheDocument();
    view.unmount();
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
