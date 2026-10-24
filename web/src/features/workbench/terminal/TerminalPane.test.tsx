// @vitest-environment jsdom
/**
 * 右栏「终端」功能标签（F2 态 B；terminal-panel.html bash 子标签行 + shell 徽标）：
 * 子标签行结构（root/bash 激活 + workspace/zsh 占位）、只读输入行占位、
 * 「关闭终端面板」回调关闭标签。输出回显复用 TerminalView（TerminalView.test 覆盖）。
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import '../../../tests/test-utils';

import { TerminalPane } from './TerminalPane';

describe('TerminalPane · 态 B bash 子标签行（F2）', () => {
  it('bash 子标签行：root/bash 激活 + workspace/zsh 占位 + 终端标签头', () => {
    render(<TerminalPane tabId="terminal:main" onCloseTab={vi.fn()} />);

    const rootTab = screen.getByTestId('terminal-pane-tab-root');
    expect(rootTab).toHaveClass('on');
    expect(rootTab).toHaveTextContent('root');
    expect(screen.getByText('bash')).toBeInTheDocument();

    const workspaceTab = screen.getByTestId('terminal-pane-tab-workspace');
    expect(workspaceTab).not.toHaveClass('on');
    expect(workspaceTab).toHaveTextContent('workspace');
    expect(screen.getByText('zsh')).toBeInTheDocument();
  });

  it('输出复用 TerminalView（空态）+ 只读输入行占位（P0 无输入通道）', () => {
    render(<TerminalPane tabId="terminal:main" onCloseTab={vi.fn()} />);
    expect(screen.getByTestId('terminal-view')).toBeInTheDocument();
    expect(screen.getByTestId('empty-state')).toHaveTextContent('终端 · 暂无输出');

    const input = screen.getByLabelText('Terminal input');
    expect(input).toHaveAttribute('readonly');
    expect(input).toHaveAttribute('title', 'P0 只读回显，交互输入归 P1');
  });

  it('「关闭终端面板」回调携带标签 id（store closeTab 由 WorkbenchPanel 接线）', async () => {
    const user = userEvent.setup();
    const onCloseTab = vi.fn();
    render(<TerminalPane tabId="terminal:main" onCloseTab={onCloseTab} />);
    await user.click(screen.getByTestId('terminal-pane-close'));
    expect(onCloseTab).toHaveBeenCalledWith('terminal:main');

    // 新建终端为 P1 占位（aria-disabled，无行为）
    expect(screen.getByTitle('新建终端（P1 多会话占位）')).toHaveAttribute('aria-disabled', 'true');
  });
});
