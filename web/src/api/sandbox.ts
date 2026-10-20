/**
 * 沙箱端点（A4 §7.2 sandbox.ts 行；契约 ENDPOINTS：GET /sessions/{id}/sandbox）。
 *
 * 触发点：沙箱状态 / 费用估算（AC-015-4）；与 terminalStore 的 lifecycle 事件
 * （sandbox_output(channel=lifecycle)）对账——事件记**迁移动作**，本端点给**稳态投影**
 * （契约注释：resumed/recreated → running；none = 尚无沙箱实例）。
 */

import { SandboxStatusSchema } from '@contracts/rest';
import type { SandboxStatus } from '@contracts/rest';

import { request, type CallOptions } from './http';

export function getSandboxStatus(
  sessionId: string,
  options: CallOptions = {},
): Promise<SandboxStatus> {
  return request({
    path: `/sessions/${encodeURIComponent(sessionId)}/sandbox`,
    schema: SandboxStatusSchema,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
}
