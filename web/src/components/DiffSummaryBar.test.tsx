// @vitest-environment jsdom
/**
 * DiffSummaryBar 组件测试：摘要条展示 additions/deletions/文件数/round/
 * baseline，binary 文件数单独提示；零 binary 时不出现该提示。
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { DiffReadyEvent } from '@contracts/events';

import '../tests/test-utils';

import { DiffSummaryBar } from './DiffSummaryBar';

type FileSummary = DiffReadyEvent['files'][number];

function file(overrides: Partial<FileSummary> = {}): FileSummary {
  return { path: 'a.ts', change: 'modified', additions: 1, deletions: 0, binary: false, old_path: null, ...overrides };
}

const BASELINE: DiffReadyEvent['baseline'] = {
  kind: 'init',
  ref: 'lcode-baseline',
  commit: 'a'.repeat(40),
};

describe('DiffSummaryBar · 摘要展示', () => {
  it('展示 +/- 总量、文件数、round 与 baseline（kind/ref）', () => {
    render(
      <DiffSummaryBar
        round={2}
        baseline={BASELINE}
        files={[file(), file({ additions: 2, deletions: 3 })]}
        additionsTotal={3}
        deletionsTotal={3}
      />,
    );
    expect(screen.getByTestId('diff-summary-add')).toHaveTextContent('+3');
    expect(screen.getByTestId('diff-summary-del')).toHaveTextContent('-3');
    expect(screen.getByTestId('diff-summary-files')).toHaveTextContent('2');
    expect(screen.getByTestId('diff-summary-round')).toHaveTextContent('2');
    expect(screen.getByTestId('diff-summary-baseline')).toHaveTextContent('init');
    expect(screen.getByTestId('diff-summary-baseline')).toHaveTextContent('lcode-baseline');
  });

  it('binary 文件数 > 0 时单独提示；为 0 时不出现', () => {
    const { rerender } = render(
      <DiffSummaryBar
        round={1}
        baseline={{ kind: 'head', ref: 'HEAD', commit: 'b'.repeat(40) }}
        files={[file(), file({ path: 'x.bin', binary: true })]}
        additionsTotal={1}
        deletionsTotal={0}
      />,
    );
    expect(screen.getByTestId('diff-summary-binary')).toHaveTextContent('1');

    rerender(
      <DiffSummaryBar
        round={1}
        baseline={{ kind: 'head', ref: 'HEAD', commit: 'b'.repeat(40) }}
        files={[file()]}
        additionsTotal={1}
        deletionsTotal={0}
      />,
    );
    expect(screen.queryByTestId('diff-summary-binary')).not.toBeInTheDocument();
  });
});
