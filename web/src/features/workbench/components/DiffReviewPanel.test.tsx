// @vitest-environment jsdom
/**
 * DiffReviewPanel 组件测试（M3 Phase 4）：pending 渲染、接受/驳回提交（响应 DTO
 * 回填 + 提交中禁用）、失败文案与输入保留、驳回空意见提示、已裁决/被取代态、
 * 刷新恢复（REST 回填）、审查不存在错误态、跨会话不串台。
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http } from 'msw';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  makeDiffFile,
  makeDiffReview,
  requestLog,
  setDiffFilesFixture,
  setDiffReviewsFixture,
} from '../../../tests/msw-handlers';
import { server } from '../../../tests/test-utils';
import '../../../tests/test-utils';

import { useDiffStore } from '../store';

import { DiffReviewPanel } from './DiffReviewPanel';

const SESSION = 'sess-a';
const OTHER_SESSION = 'sess-b';
const REVIEW_ID = 'rev-1';
const REWORK_NOTE = '[系统] 返工未自动启动：会话已有运行中的任务。';

const PATCH = [
  'diff --git a/app.py b/app.py',
  'index 64d908b..6912166 100644',
  '--- a/app.py',
  '+++ b/app.py',
  '@@ -1,2 +1,3 @@',
  ' import os',
  '+import sys',
  ' ',
].join('\n');

function okEnvelope(data: unknown): Response {
  return new Response(JSON.stringify({ code: '0', message: 'ok', data }), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

/** 面板测试标准种子：store/REST fixture 同步（pending）。当前会话由 renderPanel 的 prop 下传。 */
function seedPending(overrides: Partial<ReturnType<typeof makeDiffReview>> = {}): void {
  const detail = makeDiffReview({ id: REVIEW_ID, session_id: SESSION, ...overrides });
  useDiffStore.getState().upsertFromRest(detail);
  setDiffReviewsFixture([detail]);
  setDiffFilesFixture(REVIEW_ID, [makeDiffFile({ path: 'app.py', patch: PATCH })]);
}

/** currentSessionId 缺省 = SESSION（模拟 App 层下传）。 */
function renderPanel(
  reviewId = REVIEW_ID,
  currentSessionId: string | null = SESSION,
): void {
  render(<DiffReviewPanel reviewId={reviewId} currentSessionId={currentSessionId} />);
}

beforeEach(() => {
  setDiffReviewsFixture([]);
  setDiffFilesFixture(REVIEW_ID, []);
});

describe('DiffReviewPanel · pending 渲染', () => {
  it('摘要条 + 文件行 + 裁决区（接受/驳回 + 空意见提示）；行默认收起、点开懒加载 diff', async () => {
    const user = userEvent.setup();
    seedPending();
    renderPanel();

    expect(await screen.findByTestId('diff-summary-bar')).toBeInTheDocument();
    expect(screen.getByTestId('diff-summary-round')).toHaveTextContent('第 1 轮');
    // .rv-row 行结构：Git 状态字母（modified=M）+ 文件名 + 路径段 + +N -N 徽标 + 复制钮
    const items = screen.getAllByTestId('diff-file-item');
    expect(items).toHaveLength(1);
    const row = items[0]!;
    expect(row).toHaveTextContent('app.py');
    expect(row).toHaveAttribute('data-expanded', 'false');
    expect(within(row).getByText('M')).toBeInTheDocument();
    expect(within(row).getByText('+5')).toBeInTheDocument();
    expect(within(row).getByText('-2')).toBeInTheDocument();
    expect(within(row).getByTestId('diff-file-copy')).toBeInTheDocument();

    expect(screen.getByTestId('diff-verdict-form')).toBeInTheDocument();
    expect(screen.getByTestId('diff-verdict-accept')).toHaveTextContent('接受');
    expect(screen.getByTestId('diff-verdict-reject')).toHaveTextContent('驳回');
    expect(screen.getByTestId('diff-verdict-empty-hint')).toHaveTextContent(
      '不填意见将不会自动返工',
    );
    // 行默认收起：无行内 diff；点击展开 → 懒加载渲染
    expect(screen.queryByTestId('diff-file-inline')).not.toBeInTheDocument();
    await user.click(row);
    expect(row).toHaveAttribute('data-expanded', 'true');
    expect((await screen.findAllByTestId('diff-line')).length).toBeGreaterThan(0);
  });

  it('驳回意见留空时提示可见；填写后提示消失', async () => {
    const user = userEvent.setup();
    seedPending();
    renderPanel();
    await screen.findByTestId('diff-verdict-form');

    expect(screen.getByTestId('diff-verdict-empty-hint')).toBeInTheDocument();
    await user.type(screen.getByTestId('diff-verdict-feedback'), '请补充测试');
    expect(screen.queryByTestId('diff-verdict-empty-hint')).not.toBeInTheDocument();
  });
});

describe('DiffReviewPanel · 裁决提交', () => {
  it('接受：提交中禁用双按钮、响应 DTO 回填 → 已接受态（裁决人 user）', async () => {
    const user = userEvent.setup();
    seedPending();
    server.use(
      http.post('*/api/v1/diff-reviews/:id/verdict', async ({ request }) => {
        requestLog.push({ method: 'POST', url: request.url, body: await request.json() });
        await new Promise((resolve) => setTimeout(resolve, 40));
        return okEnvelope(
          makeDiffReview({
            id: REVIEW_ID,
            session_id: SESSION,
            verdict: 'accepted',
            decided_by: 'user',
            resolved_at: '2026-10-18T00:00:10+00:00',
          }),
        );
      }),
    );
    renderPanel();
    await screen.findByTestId('diff-verdict-form');

    const accept = screen.getByTestId('diff-verdict-accept');
    await user.click(accept);
    expect(accept).toBeDisabled();
    expect(accept).toHaveTextContent('提交中…');
    expect(screen.getByTestId('diff-verdict-reject')).toBeDisabled();

    expect(await screen.findByTestId('diff-review-resolved')).toBeInTheDocument();
    expect(screen.getByTestId('diff-verdict-badge')).toHaveTextContent('已接受');
    expect(screen.getByTestId('diff-review-resolved').textContent).toContain('裁决人：user');
    expect(screen.queryByTestId('diff-verdict-form')).not.toBeInTheDocument();
    // 请求体：accepted（不带 feedback）
    expect(requestLog.at(-1)).toMatchObject({ method: 'POST', body: { verdict: 'accepted' } });
  });

  it('驳回带意见：body 带 feedback；响应 DTO 的系统注记必须展示', async () => {
    const user = userEvent.setup();
    seedPending();
    server.use(
      http.post('*/api/v1/diff-reviews/:id/verdict', async ({ request }) => {
        requestLog.push({ method: 'POST', url: request.url, body: await request.json() });
        return okEnvelope(
          makeDiffReview({
            id: REVIEW_ID,
            session_id: SESSION,
            verdict: 'rejected',
            feedback: `请补充测试\n\n${REWORK_NOTE}`,
            decided_by: 'user',
            resolved_at: '2026-10-18T00:00:10+00:00',
          }),
        );
      }),
    );
    renderPanel();
    await screen.findByTestId('diff-verdict-form');

    await user.type(screen.getByTestId('diff-verdict-feedback'), '请补充测试');
    await user.click(screen.getByTestId('diff-verdict-reject'));

    const resolved = await screen.findByTestId('diff-review-resolved');
    expect(screen.getByTestId('diff-verdict-badge')).toHaveTextContent('已驳回');
    expect(screen.getByTestId('diff-review-feedback')).toHaveTextContent(REWORK_NOTE);
    expect(resolved.textContent).toContain('裁决人：user');
    expect(requestLog.at(-1)).toMatchObject({
      method: 'POST',
      body: { verdict: 'rejected', feedback: '请补充测试' },
    });
  });

  it('提交失败：错误（code）文案 + 驳回输入保留', async () => {
    const user = userEvent.setup();
    seedPending();
    // REST fixture 已终态（他人已裁决的并发窗口）→ 409 DIFF_ALREADY_RESOLVED
    setDiffReviewsFixture([
      makeDiffReview({
        id: REVIEW_ID,
        session_id: SESSION,
        verdict: 'accepted',
        decided_by: 'user',
      }),
    ]);
    renderPanel();
    await screen.findByTestId('diff-verdict-form');

    await user.type(screen.getByTestId('diff-verdict-feedback'), '先不要合');
    await user.click(screen.getByTestId('diff-verdict-reject'));

    const error = await screen.findByTestId('diff-verdict-error');
    expect(error).toHaveTextContent('裁决提交失败（DIFF_ALREADY_RESOLVED）：该差异审查已裁决');
    // 输入保留（不因失败清空）
    expect(screen.getByTestId('diff-verdict-feedback')).toHaveValue('先不要合');
    // 裁决区仍在（可重试）
    expect(screen.getByTestId('diff-verdict-form')).toBeInTheDocument();
  });
});

describe('DiffReviewPanel · 已裁决态', () => {
  it('accepted：显示已接受徽标 + 裁决人，无裁决区', () => {
    seedPending({
      verdict: 'accepted',
      decided_by: 'user',
      resolved_at: '2026-10-18T00:00:10+00:00',
    });
    renderPanel();

    expect(screen.getByTestId('diff-verdict-badge')).toHaveTextContent('已接受');
    expect(screen.getByTestId('diff-review-resolved').textContent).toContain('裁决人：user');
    expect(screen.queryByTestId('diff-verdict-form')).not.toBeInTheDocument();
  });

  it('用户驳回：显示已驳回徽标 + 意见', () => {
    seedPending({
      verdict: 'rejected',
      feedback: '请补充测试',
      decided_by: 'user',
      resolved_at: '2026-10-18T00:00:10+00:00',
    });
    renderPanel();

    expect(screen.getByTestId('diff-verdict-badge')).toHaveTextContent('已驳回');
    expect(screen.getByTestId('diff-review-feedback')).toHaveTextContent('请补充测试');
  });

  it('被取代（rejected + system）：显示「已被新一轮审查取代」而非用户驳回', () => {
    seedPending({
      verdict: 'rejected',
      feedback: '已被第 2 轮审查取代',
      decided_by: 'system',
      resolved_at: '2026-10-18T00:00:10+00:00',
    });
    renderPanel();

    expect(screen.getByTestId('diff-verdict-badge')).toHaveTextContent('已被新一轮审查取代');
    expect(screen.getByTestId('diff-review-feedback')).toHaveTextContent('已被第 2 轮审查取代');
    expect(screen.getByTestId('diff-review-resolved').textContent).not.toContain('裁决人');
  });
});

describe('DiffReviewPanel · 刷新恢复与空态', () => {
  it('store 缺该 review（刷新页面）→ REST 全量回填', async () => {
    setDiffReviewsFixture([makeDiffReview({ id: REVIEW_ID, session_id: SESSION })]);
    setDiffFilesFixture(REVIEW_ID, [makeDiffFile({ path: 'app.py', patch: PATCH })]);
    renderPanel();

    expect(await screen.findByTestId('diff-summary-bar')).toBeInTheDocument();
    expect(useDiffStore.getState().reviews.get(REVIEW_ID)).toMatchObject({
      id: REVIEW_ID,
      sessionId: SESSION,
      verdict: 'pending',
    });
  });

  it('审查不存在（404）→ 无审查空态（B6，empty-states #1 文案照抄 + frame 头部）；补齐数据后刷新可恢复', async () => {
    const user = userEvent.setup();
    renderPanel();

    const empty = await screen.findByTestId('diff-review-empty');
    expect(empty.textContent).toContain('当前 workspace 不在 Git 仓库中');
    expect(empty.textContent).toContain('打开一个 Git 仓库目录后，这里会展示当前 workspace 作用域内的改动。');
    // frame 变体头部：未暂存 badge + git 状态过滤 + 刷新钮
    expect(empty.querySelector('.frame-head')).not.toBeNull();
    expect(empty.textContent).toContain('未暂存');
    expect(empty.textContent).toContain('git 状态过滤');
    expect(screen.getByTestId('diff-review-empty-refresh')).toBeInTheDocument();

    setDiffReviewsFixture([makeDiffReview({ id: REVIEW_ID, session_id: SESSION })]);
    setDiffFilesFixture(REVIEW_ID, [makeDiffFile({ path: 'app.py', patch: PATCH })]);
    await user.click(screen.getByTestId('diff-review-empty-refresh'));

    await waitFor(() => expect(screen.getByTestId('diff-summary-bar')).toBeInTheDocument());
  });

  it('其余错误（非 404）仍走错误空态：文案含错误码与重试钮（B6 行为分支保留）', async () => {
    server.use(
      http.get('*/api/v1/diff-reviews/:id', () =>
        new Response(JSON.stringify({ code: 'INTERNAL_ERROR', message: 'boom' }), {
          status: 500,
          headers: { 'content-type': 'application/json' },
        }),
      ),
    );
    renderPanel();

    const empty = await screen.findByTestId('empty-state');
    expect(empty.textContent).toContain('INTERNAL_ERROR');
    expect(screen.getByTestId('diff-review-retry')).toBeInTheDocument();
  });

  it('跨会话审查不串台：store 里的 review 属其它会话 → 空态提示，不渲染内容', () => {
    useDiffStore
      .getState()
      .upsertFromRest(makeDiffReview({ id: REVIEW_ID, session_id: OTHER_SESSION }));
    renderPanel(REVIEW_ID, SESSION);

    expect(screen.getByTestId('empty-state')).toHaveTextContent('该审查属于其它会话');
    expect(screen.queryByTestId('diff-summary-bar')).not.toBeInTheDocument();
    expect(screen.queryByTestId('diff-verdict-form')).not.toBeInTheDocument();
  });

  it('跨会话审查不串台：REST 回填发现会话不符 → 只提示，不写入当前会话 store', async () => {
    setDiffReviewsFixture([makeDiffReview({ id: REVIEW_ID, session_id: OTHER_SESSION })]);
    renderPanel(REVIEW_ID, SESSION);

    await screen.findByTestId('empty-state');
    expect(screen.getByTestId('empty-state')).toHaveTextContent('该审查属于其它会话');
    expect(useDiffStore.getState().reviews.get(REVIEW_ID)).toBeUndefined();
  });
});

describe('DiffReviewPanel · 文件行交互（行内展开 + 复制路径）', () => {
  it('点选其它行：该行行内展开自身 diff（懒加载），已展开行互不影响', async () => {
    const user = userEvent.setup();
    seedPending();
    useDiffStore.getState().upsertFromRest(
      makeDiffReview({
        id: REVIEW_ID,
        session_id: SESSION,
        files: [
          { path: 'app.py', old_path: null, change: 'modified', additions: 5, deletions: 2, binary: false },
          { path: 'new.ts', old_path: null, change: 'added', additions: 9, deletions: 0, binary: false },
        ],
      }),
    );
    setDiffFilesFixture(REVIEW_ID, [
      makeDiffFile({ path: 'app.py', patch: PATCH }),
      makeDiffFile({
        path: 'new.ts',
        change: 'added',
        additions: 9,
        deletions: 0,
        patch: 'diff --git a/new.ts b/new.ts\n--- /dev/null\n+++ b/new.ts\n@@ -0,0 +1,1 @@\n+export {};\n',
      }),
    ]);
    renderPanel();

    const items = await screen.findAllByTestId('diff-file-item');
    expect(items).toHaveLength(2);
    // 默认全部收起（行内展开模型）
    for (const item of items) expect(item).toHaveAttribute('data-expanded', 'false');

    await user.click(items[1]!);
    // 第二行展开自己的行内 diff（.rv-diff），文件头展示对应路径与 Git 状态字母
    const inline = await screen.findByTestId('diff-file-inline');
    const header = within(inline).getByTestId('diff-file-header');
    expect(header.textContent).toContain('new.ts');
    expect(within(header).getByText('A')).toBeInTheDocument();
    expect(within(inline).getAllByTestId('diff-line').length).toBeGreaterThan(0);
    // 第一行仍收起
    expect(items[0]).toHaveAttribute('data-expanded', 'false');
  });

  it('复制路径：写入剪贴板并反馈「已复制」，不触发行展开', async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.spyOn(navigator, 'clipboard', 'get').mockReturnValue({ writeText } as unknown as Clipboard);
    seedPending();
    renderPanel();

    const row = (await screen.findAllByTestId('diff-file-item'))[0]!;
    await user.click(within(row).getByTestId('diff-file-copy'));
    expect(writeText).toHaveBeenCalledWith('app.py');
    await waitFor(() => expect(within(row).getByTestId('diff-file-copy')).toHaveTextContent('已复制'));
    expect(row).toHaveAttribute('data-expanded', 'false');
    expect(screen.queryByTestId('diff-file-inline')).not.toBeInTheDocument();
  });

  it('键盘可达：行聚焦后 Enter/空格展开（role=button）', async () => {
    const user = userEvent.setup();
    seedPending();
    renderPanel();

    const row = (await screen.findAllByTestId('diff-file-item'))[0]!;
    expect(row).toHaveAttribute('role', 'button');
    row.focus();
    await user.keyboard('{Enter}');
    expect(row).toHaveAttribute('data-expanded', 'true');
    expect(await screen.findByTestId('diff-file-inline')).toBeInTheDocument();
  });
});
