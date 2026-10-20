/**
 * 会话端点（A4 §7.2 sessions.ts 行；契约 ENDPOINTS：POST/GET /sessions、
 * GET/PATCH/DELETE /sessions/{id}）。
 *
 * 触发点：新建会话、左栏列表（AC-004-3）、会话切换种子（run_state 刷新恢复）、
 * 删除（kill 沙箱联动 AC-002-3）、模型/档位热切换（PATCH，AC-014-2）。
 */

import {
  SessionDetailSchema,
  SessionPageSchema,
} from '@contracts/rest';
import type { SessionCreate, SessionDetail, SessionPage, SessionPatch } from '@contracts/rest';

import { request, type CallOptions } from './http';

export function listSessions(
  options: { readonly title?: string } & CallOptions = {},
): Promise<SessionPage> {
  return request({
    path: '/sessions',
    query: { title: options.title },
    schema: SessionPageSchema,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
}

export function getSession(sessionId: string, options: CallOptions = {}): Promise<SessionDetail> {
  return request({
    path: `/sessions/${encodeURIComponent(sessionId)}`,
    schema: SessionDetailSchema,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
}

export function createSession(
  body: SessionCreate,
  options: CallOptions = {},
): Promise<SessionDetail> {
  return request({
    path: '/sessions',
    method: 'POST',
    body,
    schema: SessionDetailSchema,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
}

export function patchSession(
  sessionId: string,
  body: SessionPatch,
  options: CallOptions = {},
): Promise<SessionDetail> {
  return request({
    path: `/sessions/${encodeURIComponent(sessionId)}`,
    method: 'PATCH',
    body,
    schema: SessionDetailSchema,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
}

/** 删除会话（同步 kill 沙箱；204 无 body）。 */
export function deleteSession(sessionId: string, options: CallOptions = {}): Promise<void> {
  return request<void>({
    path: `/sessions/${encodeURIComponent(sessionId)}`,
    method: 'DELETE',
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
}
