/**
 * diff 审查端点（M3 Phase 4；契约 ENDPOINTS：GET /diff-reviews/{id}、
 * GET /diff-reviews/{id}/files/{file_path}、POST /diff-reviews/{id}/verdict）。
 *
 * 触发点：
 * - getDiffReview：审查面板刷新恢复（store 缺该 review 时一次性全量回填）；
 * - getDiffFile：单文件 patch 懒加载（面板选中某文件才请求，命中缓存不再请求）；
 * - postVerdict：裁决提交（accepted 推进基线 / rejected 保存意见并按需自动返工），
 *   重复裁决由后端 DIFF_ALREADY_RESOLVED 兜底（幂等 409 经 ApiError 透传）。
 *
 * 响应一律经契约 zod schema 校验（DiffReviewDetail / DiffFileDetail，@contracts
 * 单源，禁止手写 wire 形状）。
 *
 * **file_path 编码规则**：契约钉死 `file_path` 为含 `/` 的路径参数（后端
 * `:path` 路由转换器承接）——必须逐段 encodeURIComponent 后以 `/` 拼回，整体
 * encode 会把分隔符变成 `%2F` 导致路由失配。
 */

import {
  DiffFileDetailSchema,
  DiffReviewDetailSchema,
} from '@contracts/rest';
import type {
  DiffFileDetail,
  DiffReviewDetail,
  DiffVerdictRequest,
} from '@contracts/rest';

import { request, type CallOptions } from './http';

/** GET /diff-reviews/{review_id}（审查详情：轮次/基线/逐文件摘要/totals/裁决态）。 */
export function getDiffReview(reviewId: string, options: CallOptions = {}): Promise<DiffReviewDetail> {
  return request({
    path: `/diff-reviews/${encodeURIComponent(reviewId)}`,
    schema: DiffReviewDetailSchema,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
}

/** GET /diff-reviews/{review_id}/files/{file_path}（单文件完整 patch；binary/超限截断为 patch=null + blob_ref）。 */
export function getDiffFile(
  reviewId: string,
  filePath: string,
  options: CallOptions = {},
): Promise<DiffFileDetail> {
  // 逐段编码：保留 `/` 作为路径分隔（契约端点 summary 明示 file_path 含 `/`）
  const encodedPath = filePath.split('/').map(encodeURIComponent).join('/');
  return request({
    path: `/diff-reviews/${encodeURIComponent(reviewId)}/files/${encodedPath}`,
    schema: DiffFileDetailSchema,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
}

/** POST /diff-reviews/{review_id}/verdict（裁决；成功回执含落库后的审查详情）。 */
export function postVerdict(
  reviewId: string,
  body: DiffVerdictRequest,
  options: CallOptions = {},
): Promise<DiffReviewDetail> {
  return request({
    path: `/diff-reviews/${encodeURIComponent(reviewId)}/verdict`,
    method: 'POST',
    body,
    schema: DiffReviewDetailSchema,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
}
