// @vitest-environment jsdom
/**
 * 回放 · 沙箱生命周期与终端回显（AC-002-1/2；test-plan §2.2）：
 * sandbox_output stdout（带 exec_id）→ TerminalView 实时行 + exec 标注；
 * lifecycle paused/resumed → 生命周期徽标两态；stdout 缺 exec_id → unattributed
 * 容忍桶（B 档：不丢内容，如实标注来源缺失）。
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import lifecyclePaused from '@contracts-fixtures/events/valid/sandbox_output_lifecycle.json';
import sandboxOutput from '@contracts-fixtures/events/valid/sandbox_output.json';

import { useTerminalStore } from '@/features/workbench/store';
import { TerminalView } from '@/features/workbench/terminal/TerminalView';

import { eventBase, feedFixtures } from './helpers';
import '../../tests/test-utils';

describe('replay/sandbox-lifecycle：终端实时回显与生命周期徽标', () => {
  it('stdout 行实时 append 并带 exec 标注（100ms 限频后的落地结果可直接断言）', () => {
    feedFixtures([
      sandboxOutput, // stdout：exec call_a1b2c3，pytest 进度行
      eventBase(113, 'sandbox_output', {
        sandbox_id: 'sbx_9f8e7d',
        exec_id: 'call_a1b2c3',
        channel: 'stdout',
        data: '4 passed in 1.2s\n',
      }),
    ]);

    render(<TerminalView />);
    expect(screen.getByTestId('terminal-view')).toBeInTheDocument();
    const execBlock = screen.getByTestId('terminal-exec');
    expect(execBlock).toHaveAttribute('data-exec-id', 'call_a1b2c3');
    expect(screen.getByTestId('terminal-exec-label')).toHaveTextContent('exec · call_a1b2c3');
    expect(screen.getAllByTestId('terminal-line')).toHaveLength(2);
    expect(screen.getByTestId('terminal-lines')).toHaveTextContent('4 passed in 1.2s');
  });

  it('lifecycle paused → 「沙箱已暂停」徽标；resumed → 运行中（AC-002-2 可见性）', () => {
    feedFixtures([lifecyclePaused]);
    render(<TerminalView />);
    const chip = screen.getByTestId('terminal-lifecycle');
    expect(chip).toHaveAttribute('data-phase', 'paused');
    expect(chip).toHaveTextContent('沙箱已暂停');
    expect(chip).toHaveTextContent('bailian');

    feedFixtures([
      eventBase(151, 'sandbox_output', {
        sandbox_id: 'sbx_9f8e7d',
        exec_id: null,
        channel: 'lifecycle',
        data: '',
        phase: 'resumed',
        provider: 'bailian',
      }),
    ]);
    expect(screen.getByTestId('terminal-lifecycle')).toHaveAttribute('data-phase', 'resumed');
    expect(screen.getByTestId('terminal-lifecycle')).toHaveTextContent('沙箱运行中');
  });

  it('B 档容忍：stdout 缺 exec_id 落 unattributed 桶（不丢内容，标注来源缺失）', () => {
    feedFixtures([
      eventBase(114, 'sandbox_output', {
        sandbox_id: 'sbx_9f8e7d',
        exec_id: null,
        channel: 'stdout',
        data: 'orphan output\n',
      }),
    ]);
    render(<TerminalView />);
    const block = screen.getByTestId('terminal-exec');
    expect(block).toHaveAttribute('data-exec-id', 'unattributed');
    expect(screen.getByTestId('terminal-lines')).toHaveTextContent('orphan output');
  });

  it('环形上限：超 2000 行滚存最早的行并留计数（store 侧保证，UI 出滚存标注）', () => {
    const first = ['line-first'];
    const filler: string[] = [];
    for (let i = 0; i < 2000; i += 1) filler.push(`row-${i}`);
    useTerminalStore.getState().appendLines('exec-cap', first);
    useTerminalStore.getState().appendLines('exec-cap', filler);

    expect(useTerminalStore.getState().execs.get('exec-cap')?.lines).toHaveLength(2000);
    expect(useTerminalStore.getState().execs.get('exec-cap')?.droppedLines).toBe(1);

    render(<TerminalView />);
    expect(screen.getByTestId('terminal-dropped')).toHaveTextContent('已滚存最早 1 行');
    expect(screen.getByTestId('terminal-lines')).not.toHaveTextContent('line-first');
    expect(screen.getByTestId('terminal-lines')).toHaveTextContent('row-1999');
  });
});
