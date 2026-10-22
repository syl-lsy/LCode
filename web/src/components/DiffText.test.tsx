// @vitest-environment jsdom
/**
 * DiffText 组件测试（M3 Phase 4 diff 审查地基）：
 * 加/删行样式可区分（design token 类断言）、双列行号、hunk section、
 * binary 占位、unparseable 空态、no-newline 可见标记、大 hunk 折叠。
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import '../tests/test-utils';

import { DiffText, HUNK_COLLAPSE_LINE_THRESHOLD } from './DiffText';

/** 真实 git diff 样本（同 diffParse.test.ts，两 hunk 带 section） */
const MODIFY_TWO_HUNKS = `diff --git a/app.py b/app.py
index 64d908b..6912166 100644
--- a/app.py
+++ b/app.py
@@ -1,8 +1,9 @@
 import os
+import sys


 def alpha():
-    return 1
+    return 10


 def beta():
@@ -10,4 +11,4 @@ def beta():


 def gamma():
-    return 3
+    return 33
`;

/** no-newline 在删除行后 */
const NO_NEWLINE_OLD_SIDE = `diff --git a/onl.txt b/onl.txt
index 832f7c0..4198eb9 100644
--- a/onl.txt
+++ b/onl.txt
@@ -1,2 +1,2 @@
 p1
-p2 no trailing
\\ No newline at end of file
+p2 now has newline
`;

describe('DiffText · 行渲染', () => {
  it('加/删行样式可区分：data-kind + design token 底色类', () => {
    render(<DiffText path="app.py" binary={false} patch={MODIFY_TWO_HUNKS} />);
    const addRows = screen.getAllByTestId('diff-line').filter((el) => el.dataset.kind === 'add');
    const delRows = screen.getAllByTestId('diff-line').filter((el) => el.dataset.kind === 'del');
    expect(addRows.length).toBe(3);
    expect(delRows.length).toBe(2);
    // token 类：加行绿底 / 删行红底，互不相同
    expect(addRows[0]).toHaveClass('bg-diff-added-bg');
    expect(delRows[0]).toHaveClass('bg-diff-removed-bg');
    // 前缀符可见
    expect(addRows[0]!.textContent).toContain('+');
    expect(delRows[0]!.textContent).toContain('-');
  });

  it('双列行号：context 双号、add 只有 new、del 只有 old', () => {
    render(<DiffText path="app.py" binary={false} patch={MODIFY_TWO_HUNKS} />);
    const rows = screen.getAllByTestId('diff-line');
    const ctx = rows.find((el) => el.dataset.kind === 'context')!;
    expect(ctx.querySelector('[data-testid="diff-ln-old"]')).toHaveTextContent('1');
    expect(ctx.querySelector('[data-testid="diff-ln-new"]')).toHaveTextContent('1');

    const add = rows.find((el) => el.dataset.kind === 'add')!;
    expect(add.querySelector('[data-testid="diff-ln-old"]')).toHaveTextContent('');
    expect(add.querySelector('[data-testid="diff-ln-new"]')).toHaveTextContent('2');

    const del = rows.find((el) => el.dataset.kind === 'del')!;
    expect(del.querySelector('[data-testid="diff-ln-old"]')).toHaveTextContent('5');
    expect(del.querySelector('[data-testid="diff-ln-new"]')).toHaveTextContent('');
  });

  it('hunk 头分隔行展示 section（有则显示，无则只显示区间）', () => {
    render(<DiffText path="app.py" binary={false} patch={MODIFY_TWO_HUNKS} />);
    const sections = screen.getAllByTestId('diff-hunk-header');
    expect(sections).toHaveLength(2);
    expect(sections[0]).toHaveTextContent('-1,8');
    expect(sections[0]).toHaveTextContent('+1,9');
    expect(screen.getAllByTestId('diff-section')[0]).toHaveTextContent('def beta():');
  });

  it('noNewline 行有可见标记且只标在挂载行', () => {
    render(<DiffText path="onl.txt" binary={false} patch={NO_NEWLINE_OLD_SIDE} />);
    const marks = screen.getAllByTestId('diff-no-newline');
    expect(marks).toHaveLength(1);
    // 标记所在行是删除行（p2 no trailing）
    const delRow = marks[0]!.closest<HTMLElement>('[data-testid="diff-line"]')!;
    expect(delRow.dataset.kind).toBe('del');
    expect(delRow.textContent).toContain('p2 no trailing');
  });
});

describe('DiffText · 占位与错误态', () => {
  it('binary：渲染占位不白屏，有 blob_ref 时展示引用', () => {
    render(
      <DiffText path="blob.bin" binary blobRef="session-1/blob.bin" patch={null} />,
    );
    expect(screen.getByTestId('diff-binary')).toHaveTextContent('二进制文件');
    expect(screen.getByTestId('diff-binary')).toHaveTextContent('session-1/blob.bin');
  });

  it('binary 无 blob_ref：只显示占位文案', () => {
    render(<DiffText path="blob.bin" binary blobRef={null} patch={null} />);
    expect(screen.getByTestId('diff-binary')).toHaveTextContent('二进制文件');
  });

  it('unparseable：明确错误态，不出行渲染', () => {
    render(<DiffText path="bad.txt" binary={false} patch="@@ 这不是合法 hunk\n垃圾\n" />);
    expect(screen.getByTestId('diff-error')).toBeInTheDocument();
    expect(screen.queryByTestId('diff-line')).not.toBeInTheDocument();
  });

  it('空 patch：空态不白屏', () => {
    render(<DiffText path="empty.txt" binary={false} patch="" />);
    expect(screen.getByTestId('diff-error')).toBeInTheDocument();
  });
});

describe('DiffText · 大 hunk 折叠', () => {
  it('超过阈值的 hunk 默认折叠，展开按钮点开后渲染全部行', async () => {
    const user = userEvent.setup();
    const big = `diff --git a/big.txt b/big.txt
index 1111111..2222222 100644
--- a/big.txt
+++ b/big.txt
@@ -1,${HUNK_COLLAPSE_LINE_THRESHOLD + 1} +1,${HUNK_COLLAPSE_LINE_THRESHOLD + 1} @@
${Array.from({ length: HUNK_COLLAPSE_LINE_THRESHOLD + 1 }, (_, i) => ` line ${i + 1}`).join('\n')}
`;
    render(<DiffText path="big.txt" binary={false} patch={big} />);

    const hunk = screen.getByTestId('diff-hunk');
    expect(hunk).toHaveAttribute('data-collapsed', 'true');
    expect(screen.queryByTestId('diff-line')).not.toBeInTheDocument();

    await user.click(screen.getByTestId('diff-hunk-toggle'));
    expect(hunk).toHaveAttribute('data-collapsed', 'false');
    expect(screen.getAllByTestId('diff-line')).toHaveLength(HUNK_COLLAPSE_LINE_THRESHOLD + 1);
  });

  it('小于阈值的 hunk 不折叠', () => {
    render(<DiffText path="app.py" binary={false} patch={MODIFY_TWO_HUNKS} />);
    expect(screen.getAllByTestId('diff-hunk').every((el) => el.dataset.collapsed === 'false')).toBe(true);
    expect(screen.queryByTestId('diff-hunk-toggle')).not.toBeInTheDocument();
  });
});
