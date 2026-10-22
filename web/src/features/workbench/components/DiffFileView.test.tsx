// @vitest-environment jsdom
/**
 * DiffFileView 组件测试（M3 Phase 4）：patch 懒加载（选中才请求、缓存命中不再
 * 请求）、三种非文本形态区分（binary / 超大截断 patch=null+blob_ref / 空 patch）、
 * 加载失败重试。
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';

import {
  DIFF_FILES_ROUTE,
  makeDiffFile,
  makeDiffReview,
  requestLog,
  setDiffFilesFixture,
  setDiffReviewsFixture,
} from '../../../tests/msw-handlers';
import '../../../tests/test-utils';

import { server } from '../../../tests/test-utils';

import { useDiffStore } from '../store';
import { DiffFileView } from './DiffFileView';

const REVIEW_ID = 'rev-1';
const PATCH = [
  'diff --git a/app.py b/app.py',
  'index 64d908b..6912166 100644',
  '--- a/app.py',
  '+++ b/app.py',
  '@@ -1,3 +1,4 @@',
  ' import os',
  '+import sys',
  ' ',
  ' def main():',
].join('\n');

function filesRequestCount(): number {
  return requestLog.filter((entry) => entry.method === 'GET' && entry.url.includes('/files/')).length;
}

function seedReview(path = 'app.py'): void {
  useDiffStore.getState().upsertFromRest(
    makeDiffReview({
      id: REVIEW_ID,
      session_id: 'sess-a',
      files: [
        { path, old_path: null, change: 'modified', additions: 5, deletions: 2, binary: false },
      ],
    }),
  );
}

function summaryOf(path = 'app.py'): React.ComponentProps<typeof DiffFileView>['summary'] {
  return { path, old_path: null, change: 'modified', additions: 5, deletions: 2, binary: false };
}

beforeEach(() => {
  seedReview();
  setDiffReviewsFixture([makeDiffReview({ id: REVIEW_ID })]);
});

describe('DiffFileView · patch 懒加载', () => {
  it('选中时请求一次：渲染 diff 行并写入 store 缓存', async () => {
    setDiffFilesFixture(REVIEW_ID, [makeDiffFile({ path: 'app.py', patch: PATCH })]);

    render(<DiffFileView reviewId={REVIEW_ID} summary={summaryOf()} />);

    expect((await screen.findAllByTestId('diff-line')).length).toBeGreaterThan(0);
    expect(filesRequestCount()).toBe(1);
    // 结果写 useDiffStore.cachePatch
    expect(useDiffStore.getState().reviews.get(REVIEW_ID)?.patches.get('app.py')?.patch).toBe(PATCH);
  });

  it('已有缓存不再请求（第二次渲染命中缓存）', async () => {
    // requestLog 为模块级累计数组：以本用例起点为基线做相对断言
    const before = filesRequestCount();
    useDiffStore.getState().cachePatch(REVIEW_ID, makeDiffFile({ path: 'app.py', patch: PATCH }));

    render(<DiffFileView reviewId={REVIEW_ID} summary={summaryOf()} />);

    expect((await screen.findAllByTestId('diff-line')).length).toBeGreaterThan(0);
    expect(filesRequestCount()).toBe(before);
  });

  it('rename：头部展示 old_path → path', async () => {
    setDiffFilesFixture(REVIEW_ID, [makeDiffFile({ path: 'renamed.py', patch: PATCH })]);
    useDiffStore.getState().upsertFromRest(
      makeDiffReview({
        id: REVIEW_ID,
        session_id: 'sess-a',
        files: [
          {
            path: 'renamed.py',
            old_path: 'old-name.py',
            change: 'renamed',
            additions: 5,
            deletions: 2,
            binary: false,
          },
        ],
      }),
    );

    render(
      <DiffFileView
        reviewId={REVIEW_ID}
        summary={{
          path: 'renamed.py',
          old_path: 'old-name.py',
          change: 'renamed',
          additions: 5,
          deletions: 2,
          binary: false,
        }}
      />,
    );

    const header = await screen.findByTestId('diff-file-header');
    expect(header.textContent).toContain('old-name.py → renamed.py');
  });
});

describe('DiffFileView · 三种非文本形态', () => {
  it('binary：占位提示 + blob_ref 展示', async () => {
    useDiffStore.getState().cachePatch(
      REVIEW_ID,
      makeDiffFile({
        path: 'app.py',
        binary: true,
        patch: null,
        blob_ref: 'sandbox:/workspace/app.py',
      }),
    );

    render(<DiffFileView reviewId={REVIEW_ID} summary={summaryOf()} />);

    const placeholder = await screen.findByTestId('diff-binary');
    expect(placeholder.textContent).toContain('二进制文件，无法展示文本 diff');
    expect(placeholder.textContent).toContain('sandbox:/workspace/app.py');
    expect(screen.queryByTestId('diff-error')).not.toBeInTheDocument();
  });

  it('超大截断（binary=false + patch=null + blob_ref）：提示「文件过大，未内联展示」而非空文件/错误', async () => {
    useDiffStore.getState().cachePatch(
      REVIEW_ID,
      makeDiffFile({
        path: 'app.py',
        binary: false,
        patch: null,
        blob_ref: 'sandbox:/workspace/huge.py',
      }),
    );

    render(<DiffFileView reviewId={REVIEW_ID} summary={summaryOf()} />);

    const oversize = await screen.findByTestId('diff-file-oversize');
    expect(oversize.textContent).toContain('文件过大，未内联展示');
    expect(oversize.textContent).toContain('sandbox:/workspace/huge.py');
    expect(screen.queryByTestId('diff-error')).not.toBeInTheDocument();
    expect(screen.queryByTestId('diff-binary')).not.toBeInTheDocument();
  });

  it('空 patch（真空 diff，如仅 mode 变化）：明确「无内容变更」', async () => {
    useDiffStore.getState().cachePatch(
      REVIEW_ID,
      makeDiffFile({ path: 'app.py', binary: false, patch: '' }),
    );

    render(<DiffFileView reviewId={REVIEW_ID} summary={summaryOf()} />);

    const noChange = await screen.findByTestId('diff-file-nochange');
    expect(noChange.textContent).toContain('无内容变更');
    expect(screen.queryByTestId('diff-error')).not.toBeInTheDocument();
  });
});

describe('DiffFileView · 加载失败与重试', () => {
  it('请求失败：错误态 + 重试按钮；重试成功后渲染 diff', async () => {
    const user = userEvent.setup();
    server.use(
      http.get(DIFF_FILES_ROUTE, () => {
        return new Response(JSON.stringify({ code: 'INTERNAL_ERROR', message: 'boom' }), {
          status: 500,
          headers: { 'content-type': 'application/json' },
        });
      }),
    );

    render(<DiffFileView reviewId={REVIEW_ID} summary={summaryOf()} />);

    const errorPane = await screen.findByTestId('diff-file-error');
    expect(errorPane.textContent).toContain('boom');
    expect(errorPane.textContent).toContain('重试');

    // 恢复默认 handler → 重试成功
    server.resetHandlers();
    setDiffFilesFixture(REVIEW_ID, [makeDiffFile({ path: 'app.py', patch: PATCH })]);
    await user.click(screen.getByTestId('diff-file-retry'));

    await waitFor(() =>
      expect(screen.getAllByTestId('diff-line').length).toBeGreaterThan(0),
    );
    expect(screen.queryByTestId('diff-file-error')).not.toBeInTheDocument();
  });
});
