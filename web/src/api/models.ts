/**
 * 模型端点（A4 §7.2 models.ts 行；契约 ENDPOINTS：GET /models）。
 *
 * 触发点：模型选择器 / 档位选择器（AC-014-1/3）——`reasoning_efforts` 是按模型
 * param_support 过滤后的档位集合，前端据此置灰（不暴露采样参数，AGENTS.md 硬约束）。
 */

import { ModelListSchema } from '@contracts/rest';
import type { ModelList } from '@contracts/rest';

import { request, type CallOptions } from './http';

export function listModels(options: CallOptions = {}): Promise<ModelList> {
  return request({
    path: '/models',
    schema: ModelListSchema,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
}
