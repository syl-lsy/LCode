/**
 * 消息与运行端点（A4 §7.2 messages.ts 行；契约 ENDPOINTS：POST /sessions/{id}/messages、
 * GET /sessions/{id}/messages（游标分页）、POST /runs/{id}/stop）。
 *
 * 触发点：Composer 发送（AC-001-1/4，附 MessageSendAck.event_seq 水位 →
 * streamManager.ensureCaughtUp）、历史导出/检索、停止 run（AC-001-2 幂等）。
 * 面板重建不走本端点（走事件重放，REQ-008/AC-008-1）。
 */

import { MessagePageSchema, MessageSendAckSchema, StopRunAckSchema } from '@contracts/rest';
import type { MessageCreate, MessagePage, MessageSendAck, StopRunAck } from '@contracts/rest';

import { request, type CallOptions } from './http';

/** 发送用户消息（启动 run）；返回的 event_seq 即订阅水位（§4.2 路径 B）。 */
export function sendMessage(
  sessionId: string,
  body: MessageCreate,
  options: CallOptions = {},
): Promise<MessageSendAck> {
  return request({
    path: `/sessions/${encodeURIComponent(sessionId)}/messages`,
    method: 'POST',
    body,
    schema: MessageSendAckSchema,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
}

/** 消息历史（游标分页；next_cursor 为空表示尾页，A4 §7.3）。 */
export function listMessages(
  sessionId: string,
  options: { readonly cursor?: string } & CallOptions = {},
): Promise<MessagePage> {
  return request({
    path: `/sessions/${encodeURIComponent(sessionId)}/messages`,
    query: { cursor: options.cursor },
    schema: MessagePageSchema,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
}

/** 停止运行（幂等：StopRunAck.status 恒 "stopped"；已收内容由前端保留，AC-001-2）。 */
export function stopRun(runId: string, options: CallOptions = {}): Promise<StopRunAck> {
  return request({
    path: `/runs/${encodeURIComponent(runId)}/stop`,
    method: 'POST',
    schema: StopRunAckSchema,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
}
