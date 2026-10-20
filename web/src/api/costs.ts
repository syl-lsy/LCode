/**
 * 成本端点（A4 §7.2 costs.ts 行；契约 ENDPOINTS：GET /sessions/{id}/costs、
 * GET /costs/summary?dimension=）。
 *
 * 触发点：会话成本卡（AC-015-3）、三维汇总（session/date/model，AC-015-2）、
 * quota 三档浮层（A4 §12 未决 8：按维度并发取数，真源仍是这两个端点）。
 */

import { CostSummarySchema, SessionCostSchema } from '@contracts/rest';
import type { CostSummary, SessionCost } from '@contracts/rest';

import { request, type CallOptions } from './http';

/** 会话成本（模型段 + 沙箱段），与事件累计对账（AC-015-3）。 */
export function getSessionCost(sessionId: string, options: CallOptions = {}): Promise<SessionCost> {
  return request({
    path: `/sessions/${encodeURIComponent(sessionId)}/costs`,
    schema: SessionCostSchema,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
}

/** 三维汇总（dimension 必填，AC-015-2）。 */
export function getCostSummary(
  dimension: CostSummary['dimension'],
  options: CallOptions = {},
): Promise<CostSummary> {
  return request({
    path: '/costs/summary',
    query: { dimension },
    schema: CostSummarySchema,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
}
