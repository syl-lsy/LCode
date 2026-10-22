/**
 * 审批端点（A4 §7.2 approvals.ts 行；契约 ENDPOINTS：GET /approvals、
 * GET /approvals/{approval_id}、POST /approvals/{approval_id}/decide）。
 *
 * 触发点：
 * - listApprovals(status=pending + session_id)：会话打开/刷新恢复挂起审批
 *   （AC-006-5；App.tsx 调用，approvalStore.upsertFromRest 合并）；
 * - decideApproval：四决策提交（REQ-006；DecisionForm 发起），重复决策由后端
 *   APPROVAL_ALREADY_RESOLVED 兜底（幂等 409 经 ApiError 透传）；
 * - getApproval：单条详情兜底。
 *
 * 响应一律经契约 zod schema 校验（ApprovalPage / ApprovalItem /
 * ApprovalDecisionAck，@contracts 单源，禁止手写 wire 形状）。
 */

import {
  ApprovalDecisionAckSchema,
  ApprovalItemSchema,
  ApprovalPageSchema,
} from '@contracts/rest';
import type {
  ApprovalDecisionAck,
  ApprovalDecisionRequest,
  ApprovalItem,
  ApprovalPage,
} from '@contracts/rest';

import { request, type CallOptions } from './http';

/** 审批状态过滤值（contracts ApprovalItem.status 全集）。 */
export type ApprovalStatusFilter = ApprovalItem['status'];

export interface ListApprovalsOptions extends CallOptions {
  /** 按状态过滤；恢复挂起卡片用 'pending'（AC-006-5） */
  readonly status?: ApprovalStatusFilter;
  /** 按会话过滤（wire 字段 session_id） */
  readonly sessionId?: string;
}

/** GET /approvals（游标分页；恢复挂起 = status=pending + session_id 过滤）。 */
export function listApprovals(options: ListApprovalsOptions = {}): Promise<ApprovalPage> {
  return request({
    path: '/approvals',
    query: { status: options.status, session_id: options.sessionId },
    schema: ApprovalPageSchema,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
}

/** GET /approvals/{id}（单条详情；APPROVAL_NOT_FOUND 由 ApiError 透传）。 */
export function getApproval(approvalId: string, options: CallOptions = {}): Promise<ApprovalItem> {
  return request({
    path: `/approvals/${encodeURIComponent(approvalId)}`,
    schema: ApprovalItemSchema,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
}

/** POST /approvals/{id}/decide（四决策 + resume；成功回执含落库后的审批行）。 */
export function decideApproval(
  approvalId: string,
  body: ApprovalDecisionRequest,
  options: CallOptions = {},
): Promise<ApprovalDecisionAck> {
  return request({
    path: `/approvals/${encodeURIComponent(approvalId)}/decide`,
    method: 'POST',
    body,
    schema: ApprovalDecisionAckSchema,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
}
