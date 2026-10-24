// @vitest-environment jsdom
/**
 * DiffCard（消息流改动文件卡；changed-files.html .files-card 结构，B3）：
 * - 行结构：主文件行（主文件名 + 文件数徽标 + 打开/选择打开方式）+ 展开开关行
 *   （chevron/文件计数/+N -N）+ 撤销行（P0 禁用占位）；
 * - 展开行内 diff（.diff-view > .file-sec，DiffFileView 懒加载）；store 缺 review
 *   时如实回退（主文件名回退轮次文案 / 明细归档提示，F15 诚实性）；
 * - 「打开」→ 审查标签；「选择打开方式」→ 文件预览占位标签（file-preview:<path>）。
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { makeDiffFile, makeDiffReview, setDiffFilesFixture, setDiffReviewsFixture } from '../../../tests/msw-handlers';
import '../../../tests/test-utils';

import { useDiffStore, useWorkbenchStore } from '@/features/workbench/store';

import { DiffCardRow } from './DiffCard';

const REVIEW_ID = 'rev-card-1';
const PRIMARY_PATH = 'obs-demo/hello.py';

const FILES = [
  { path: PRIMARY_PATH, old_path: null, change: 'added' as const, additions: 2, deletions: 0, binary: false },
  { path: 'obs-demo/README.md', old_path: null, change: 'added' as const, additions: 3, deletions: 0, binary: false },
];

const HELLO_PATCH = [
  'diff --git a/obs-demo/hello.py b/obs-demo/hello.py',
  '--- /dev/null',
  '+++ b/obs-demo/hello.py',
  '@@ -0,0 +1,2 @@',
  '+print("hi")',
  '+print(2)',
].join('\n');

function seedReview(): void {
  const detail = makeDiffReview({
    id: REVIEW_ID,
    session_id: 'sess-1',
    files: FILES,
    additions_total: 5,
    deletions_total: 0,
  });
  useDiffStore.getState().upsertFromRest(detail);
  setDiffReviewsFixture([detail]);
  setDiffFilesFixture(REVIEW_ID, [
    makeDiffFile({ path: PRIMARY_PATH, change: 'added', additions: 2, deletions: 0, patch: HELLO_PATCH }),
    makeDiffFile({
      path: 'obs-demo/README.md',
      change: 'added',
      additions: 3,
      deletions: 0,
      patch: 'diff --git a/obs-demo/README.md b/obs-demo/README.md\n--- /dev/null\n+++ b/obs-demo/README.md\n@@ -0,0 +1,1 @@\n+# obs-demo\n',
    }),
  ]);
}

function renderItem(overrides: Partial<Parameters<typeof DiffCardRow>[0]['item']> = {}): void {
  render(
    <DiffCardRow
      item={{
        kind: 'diffcard',
        id: REVIEW_ID,
        round: 2,
        fileCount: 2,
        additionsTotal: 5,
        deletionsTotal: 0,
        verdict: 'pending',
        createdAt: '2026-10-18T00:00:00+00:00',
        ...overrides,
      }}
    />,
  );
}

describe('DiffCard · 行结构（.files-card）', () => {
  it('主文件行 + 展开开关行 + 撤销行逐项就位', () => {
    seedReview();
    renderItem();

    const card = screen.getByTestId('diff-card');
    expect(card).toHaveAttribute('data-verdict', 'pending');
    // 主文件名 = 首文件 basename；文件数徽标
    expect(within(card).getByText('hello.py')).toBeInTheDocument();
    expect(within(card).getByText('2 个文件')).toBeInTheDocument();
    // 打开 / 选择打开方式
    expect(screen.getByTestId('diff-card-open')).toHaveTextContent('打开');
    expect(screen.getByTestId('diff-card-open-with')).toBeEnabled();
    // 展开开关：chevron + 文件计数 + +N -N
    const expander = screen.getByTestId('diff-card-expand');
    expect(expander).toHaveTextContent('展开已更改文件');
    expect(expander).toHaveTextContent('2 个文件已更改');
    expect(expander).toHaveTextContent('+5');
    expect(expander).toHaveTextContent('-0');
    expect(expander).toHaveAttribute('aria-expanded', 'false');
    // 撤销：P0 禁用占位
    expect(screen.getByTestId('diff-card-undo')).toBeDisabled();
    // 默认收起
    expect(screen.queryByTestId('diff-card-files')).not.toBeInTheDocument();
  });

  it('verdict 终态徽标（accepted/rejected）随 data-verdict 展示', () => {
    seedReview();
    renderItem({ verdict: 'accepted' });
    expect(screen.getByTestId('diff-card-verdict')).toHaveTextContent('已接受');
  });
});

describe('DiffCard · 展开 / 打开交互', () => {
  it('展开 → .diff-view 逐文件懒加载（file-head Git 状态字母 + 路径）', async () => {
    const user = userEvent.setup();
    seedReview();
    renderItem();

    await user.click(screen.getByTestId('diff-card-expand'));
    expect(screen.getByTestId('diff-card-expand')).toHaveAttribute('aria-expanded', 'true');
    const view = await screen.findByTestId('diff-card-files');
    expect(within(view).getAllByTestId('chat-file-head')).toHaveLength(2);
    const header = within(view).getAllByTestId('chat-file-head')[0]!;
    expect(header.textContent).toContain(PRIMARY_PATH);
    expect(within(header).getByText('A')).toBeInTheDocument();
    expect((await within(view).findAllByTestId('diff-line')).length).toBeGreaterThan(0);

    // 再点收起
    await user.click(screen.getByTestId('diff-card-expand'));
    expect(screen.queryByTestId('diff-card-files')).not.toBeInTheDocument();
  });

  it('store 缺该 review：主文件名回退轮次文案，展开给归档提示（不伪造文件列表）', async () => {
    const user = userEvent.setup();
    renderItem();

    expect(screen.getByTestId('diff-card')).toHaveTextContent('改动文件（第 2 轮）');
    expect(screen.getByTestId('diff-card-open-with')).toBeDisabled();
    await user.click(screen.getByTestId('diff-card-expand'));
    expect(screen.queryByTestId('diff-card-files')).not.toBeInTheDocument();
    expect(screen.getByText('改动明细已归档，点击「打开」在审查标签中查看。')).toBeInTheDocument();
  });

  it('「打开」→ 右栏审查标签（既有行为保留）；「选择打开方式」→ 文件预览占位标签', async () => {
    const user = userEvent.setup();
    seedReview();
    renderItem();

    await user.click(screen.getByTestId('diff-card-open'));
    expect(useWorkbenchStore.getState().activeTabId).toBe(`review:${REVIEW_ID}`);
    expect(useWorkbenchStore.getState().collapsed).toBe(false);

    await user.click(screen.getByTestId('diff-card-open-with'));
    // store 以 tabIdOf(kind,id) 落 id：placeholder:file-preview:<path>
    expect(useWorkbenchStore.getState().activeTabId).toBe(`placeholder:file-preview:${PRIMARY_PATH}`);
    const lastTab = useWorkbenchStore.getState().tabs.at(-1);
    expect(lastTab?.kind).toBe('placeholder');
    expect(lastTab?.title).toBe('hello.py');
  });
});
