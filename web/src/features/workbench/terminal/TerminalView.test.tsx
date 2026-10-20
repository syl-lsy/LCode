// @vitest-environment jsdom
/**
 * 终端视图（AC-002-1；test-plan §2.2）：
 * live append（store 追加 → DOM 实时可见）+ exec 来源标注 + 生命周期徽标 + 空态。
 * 100ms 限频在 buffers.ts 已单测（TerminalBuffer），此处断言「批量落地后回显」与标注。
 */
import { act, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { useTerminalStore } from '../store';
import '../../../tests/test-utils';

import { TerminalView } from './TerminalView';

describe('TerminalView · 实时回显与 exec 标注（AC-002-1）', () => {
  it('无输出时显示终端空态', () => {
    render(<TerminalView />);
    expect(screen.getByTestId('empty-state')).toHaveTextContent('终端 · 暂无输出');
  });

  it('live append：100ms 批量落地后行实时可见，多 exec 按到达顺序分块并标注来源', () => {
    const view = render(<TerminalView />);
    act(() => {
      useTerminalStore.getState().appendLines('call_a1b2c3', ['.... [ 82%]', '4 passed in 1.2s']);
    });
    expect(screen.getByTestId('terminal-exec')).toHaveAttribute('data-exec-id', 'call_a1b2c3');
    expect(screen.getByTestId('terminal-exec-label')).toHaveTextContent('exec · call_a1b2c3');
    expect(screen.getAllByTestId('terminal-line')).toHaveLength(2);

    act(() => {
      useTerminalStore.getState().appendLines('call_d4e5f6', ['$ ls -la', 'hello.py  README.md']);
    });
    const blocks = screen.getAllByTestId('terminal-exec');
    expect(blocks.map((b) => b.getAttribute('data-exec-id'))).toEqual(['call_a1b2c3', 'call_d4e5f6']);
    expect(screen.getByTestId('terminal-lines')).toHaveTextContent('4 passed in 1.2s');
    // toHaveTextContent 归一空白：双空格按单空格断言
    expect(screen.getByTestId('terminal-lines')).toHaveTextContent('hello.py README.md');

    // 同一 exec 追加：合并到同一块（不新开块）
    act(() => {
      useTerminalStore.getState().appendLines('call_a1b2c3', ['-- more output --']);
    });
    expect(screen.getAllByTestId('terminal-exec')).toHaveLength(2);
    const first = screen.getAllByTestId('terminal-exec')[0];
    if (first === undefined) throw new Error('首块缺失');
    expect(within(first).getAllByTestId('terminal-line')).toHaveLength(3);
    view.unmount();
  });

  it('生命周期徽标：phase 中文文案 + provider 标注；crashed 用警示色语义', () => {
    const view = render(<TerminalView />);
    act(() => {
      useTerminalStore.getState().setLifecycle({
        phase: 'crashed',
        at: '2026-10-18T00:00:00+00:00',
        provider: 'opensandbox',
        reason: 'OOM',
      });
    });
    const chip = screen.getByTestId('terminal-lifecycle');
    expect(chip).toHaveAttribute('data-phase', 'crashed');
    expect(chip).toHaveTextContent('沙箱异常退出');
    expect(chip).toHaveTextContent('opensandbox');
    expect(chip).toHaveTextContent('OOM');
    view.unmount();
  });

  it('滚存计数随环形上限留痕（droppedLines > 0 时标注）', () => {
    const lines: string[] = [];
    for (let i = 0; i < 2100; i += 1) lines.push(`row-${i}`);
    act(() => {
      useTerminalStore.getState().appendLines('call-cap', lines);
    });
    render(<TerminalView />);
    expect(screen.getByTestId('terminal-dropped')).toHaveTextContent('已滚存最早 100 行');
    const rendered = screen.getAllByTestId('terminal-line');
    expect(rendered).toHaveLength(2000);
    // 保留段从 row-100 起（row-0…row-99 被滚存丢弃）
    expect(rendered[0]).toHaveTextContent('row-100');
    expect(rendered[rendered.length - 1]).toHaveTextContent('row-2099');
  });
});
