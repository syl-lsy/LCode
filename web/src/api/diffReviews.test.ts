// @vitest-environment jsdom
/**
 * diffReviews 端点调用面（M3 Phase 4；MSW 三条端点，契约 ENDPOINTS diff-reviews 域）。
 *
 * 覆盖：GET 详情（404 DIFF_REVIEW_NOT_FOUND）、GET 单文件（file_path 含 `/` 逐段
 * 编码、文件不在该轮 → 404 NOT_FOUND）、POST 裁决（rejected 带意见 + 重复裁决
 * 409 DIFF_ALREADY_RESOLVED）。响应一律经 @contracts zod 校验（PARSE_ERROR 链路
 * 由 http.test.ts 覆盖，不在此重复）。
 */
import { beforeEach, describe, expect, it } from 'vitest';

import { requestLog, setDiffFilesFixture, setDiffReviewsFixture } from '../tests/msw-handlers';
import '../tests/test-utils';

import { ApiError } from './http';
import { getDiffFile, getDiffReview, postVerdict } from './diffReviews';

import { makeDiffFile, makeDiffReview } from '../tests/msw-handlers';

const REVIEW_ID = 'rev-d1';
const REVIEW = makeDiffReview({
  id: REVIEW_ID,
  session_id: 'sess-a',
  round: 2,
  verdict: 'pending',
});

describe('diffReviews.ts 端点面', () => {
  beforeEach(() => {
    setDiffReviewsFixture([]);
    setDiffFilesFixture(REVIEW_ID, []);
  });

  it('getDiffReview：GET /diff-reviews/{id} 返回契约 DTO', async () => {
    setDiffReviewsFixture([REVIEW]);

    await expect(getDiffReview(REVIEW_ID)).resolves.toEqual(REVIEW);
    expect(requestLog.at(-1)).toMatchObject({ method: 'GET' });
    expect(requestLog.at(-1)?.url).toContain(`/api/v1/diff-reviews/${REVIEW_ID}`);
  });

  it('getDiffReview：未知 id → ApiError(DIFF_REVIEW_NOT_FOUND)', async () => {
    await expect(getDiffReview('missing')).rejects.toMatchObject({
      name: 'ApiError',
      code: 'DIFF_REVIEW_NOT_FOUND',
    } satisfies Partial<ApiError>);
  });

  it('getDiffFile：file_path 含 `/` 逐段编码（斜杠保留为分隔符，其余按段编码）', async () => {
    setDiffReviewsFixture([REVIEW]);
    const filePath = 'docs/说明 文件.md';
    const file = makeDiffFile({
      path: filePath,
      change: 'added',
      additions: 3,
      deletions: 0,
      patch: 'diff --git a/docs/x.md b/docs/x.md\n',
    });
    setDiffFilesFixture(REVIEW_ID, [file]);

    await expect(getDiffFile(REVIEW_ID, filePath)).resolves.toEqual(file);

    const url = requestLog.at(-1)?.url ?? '';
    // 逐段编码：`/` 不被编码（否则路由匹配不到），空格/中文按段转义
    expect(url).toContain(`/files/docs/${encodeURIComponent(filePath.split('/').at(-1) ?? '')}`);
    expect(url).not.toContain('%2F');
  });

  it('getDiffFile：文件不在该轮 diff → ApiError(NOT_FOUND)', async () => {
    setDiffReviewsFixture([REVIEW]);
    setDiffFilesFixture(REVIEW_ID, [makeDiffFile({ path: 'app.py' })]);

    await expect(getDiffFile(REVIEW_ID, 'not-in-diff.py')).rejects.toMatchObject({
      name: 'ApiError',
      code: 'NOT_FOUND',
    } satisfies Partial<ApiError>);
  });

  it('postVerdict：rejected 带意见 → 返回落库后的 DiffReviewDetail', async () => {
    setDiffReviewsFixture([REVIEW]);

    const detail = await postVerdict(REVIEW_ID, {
      verdict: 'rejected',
      feedback: '请补充测试',
    });

    expect(detail).toMatchObject({
      id: REVIEW_ID,
      verdict: 'rejected',
      feedback: '请补充测试',
      decided_by: 'user',
      resolved_at: '2026-10-18T00:00:10+00:00',
    });
    expect(requestLog.at(-1)).toMatchObject({
      method: 'POST',
      body: { verdict: 'rejected', feedback: '请补充测试' },
    });
  });

  it('postVerdict：重复裁决 → ApiError(DIFF_ALREADY_RESOLVED)', async () => {
    setDiffReviewsFixture([
      makeDiffReview({ id: REVIEW_ID, verdict: 'accepted', decided_by: 'user' }),
    ]);

    await expect(postVerdict(REVIEW_ID, { verdict: 'accepted' })).rejects.toMatchObject({
      name: 'ApiError',
      code: 'DIFF_ALREADY_RESOLVED',
    } satisfies Partial<ApiError>);
  });
});
