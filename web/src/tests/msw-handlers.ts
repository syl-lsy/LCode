/**
 * REST mock handlers（A4 §10.3）：按端点表组织（A4 §7.2），信封按契约双信封格式
 * `{code:"0",message,data}`。SSE 不走 MSW——events 端点仅提供「立即关闭的空流」stub
 * 供 App 壳冒烟（流式行为一律走 fake transport，A4 §10.1）。
 *
 * fixtures 供测试断言/覆盖：`overrideHandlers()` 局部替换（server.use）。
 */

import { http, HttpResponse } from 'msw';

import type {
  CostSummary,
  ModelList,
  SandboxStatus,
  SessionCost,
  SessionDetail,
  SessionPage,
} from '@contracts/rest';

/** 测试会话（三 session 工厂：last_active_at 刻意乱序，供排序断言）。 */
export function makeSession(overrides: Partial<SessionDetail> = {}): SessionDetail {
  return {
    approval_mode: 'manual',
    created_at: '2026-10-17T09:00:00+00:00',
    id: 'sess-1',
    last_active_at: '2026-10-17T10:00:00+00:00',
    model: 'mock/qwen/qwen3-flash',
    reasoning_effort: 'medium',
    run_state: 'idle',
    sandbox_provider: 'bailian',
    status: 'active',
    title: '测试会话',
    updated_at: '2026-10-17T10:00:00+00:00',
    ...overrides,
  };
}

export const sessionA = makeSession({
  id: 'sess-a',
  title: '修复登录超时',
  last_active_at: '2026-10-17T08:00:00+00:00',
});
export const sessionB = makeSession({
  id: 'sess-b',
  title: '斐波那契函数',
  last_active_at: '2026-10-17T12:00:00+00:00',
  run_state: 'running',
});
export const sessionC = makeSession({
  id: 'sess-c',
  title: '搜索关键词样例',
  last_active_at: '2026-10-18T02:00:00+00:00',
});

export const modelListFixture: ModelList = {
  default_model: 'mock/qwen/qwen3-flash',
  models: [
    {
      id: 'mock/qwen/qwen3-flash',
      display_name: 'qwen3-flash',
      provider: 'mock',
      reasoning_efforts: ['off', 'low', 'medium', 'high'],
      supports_reasoning: true,
      is_default: true,
    },
    {
      id: 'mock/qwen/qwen3-coder-plus',
      display_name: 'qwen3-coder-plus',
      provider: 'mock',
      reasoning_efforts: ['high'],
      supports_reasoning: true,
    },
  ],
};

export const sessionCostFixture: SessionCost = {
  model: { input_tokens: 1200, output_tokens: 340 },
  model_cost: 0.42,
  sandbox: { accrued: 0.16, currency: 'CNY', rate_per_hour: 0.156 },
};

export const costByDateFixture: CostSummary = {
  dimension: 'date',
  rows: [
    {
      group_key: '2026-10-18',
      cost: 1.23,
      input_tokens: 5234,
      output_tokens: 821,
      total_tokens: 6055,
      model_calls: 3,
    },
  ],
};

export const costByModelFixture: CostSummary = {
  dimension: 'model',
  rows: [
    {
      group_key: 'mock/qwen/qwen3-flash',
      cost: 0.9,
      input_tokens: 4000,
      output_tokens: 600,
      total_tokens: 4600,
      model_calls: 2,
    },
    {
      group_key: 'mock/qwen/qwen3-coder-plus',
      cost: 0.33,
      input_tokens: 1234,
      output_tokens: 221,
      total_tokens: 1455,
      model_calls: 1,
    },
  ],
};

export const sandboxStatusFixture: SandboxStatus = {
  phase: 'running',
  provider: 'bailian',
  sandbox_id: 'sbx_test',
  running_since: '2026-10-18T01:00:00+00:00',
  cost_estimate: sessionCostFixture.sandbox,
};

export const SESSIONS_FIXTURE: readonly SessionDetail[] = [sessionA, sessionB, sessionC];

/** 契约成功/错误信封形状（供 handler 返回类型推断；错误信封无 data）。 */
interface Envelope {
  code: string;
  message?: string | null;
  data?: unknown;
}

function ok(data: unknown): HttpResponse<Envelope> {
  return HttpResponse.json<Envelope>({ code: '0', message: null, data });
}

function errorOf(status: number, code: string, message: string): HttpResponse<Envelope> {
  return HttpResponse.json<Envelope>({ code, message }, { status });
}

/** 请求记录（供「不发请求」类断言读取）。 */
export const requestLog: { method: string; url: string; body?: unknown }[] = [];

export const handlers = [
  http.get('*/api/v1/models', () => ok(modelListFixture)),

  http.get('*/api/v1/sessions', () => {
    const page: SessionPage = { items: [...SESSIONS_FIXTURE], total: SESSIONS_FIXTURE.length };
    return ok(page);
  }),
  http.post('*/api/v1/sessions', async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
    requestLog.push({ method: 'POST', url: request.url, body });
    return ok(
      makeSession({
        id: 'sess-new',
        title: '新会话',
        model: (body['model'] as string) ?? 'mock/qwen/qwen3-flash',
        reasoning_effort: (body['reasoning_effort'] as SessionDetail['reasoning_effort']) ?? 'medium',
      }),
    );
  }),

  http.get('*/api/v1/sessions/:id', ({ params }) => {
    const id = params['id'] as string;
    const found = SESSIONS_FIXTURE.find((s) => s.id === id) ?? (id === 'sess-new' ? makeSession({ id: 'sess-new', title: '新会话' }) : undefined);
    if (found === undefined) return errorOf(404, 'SESSION_NOT_FOUND', '会话不存在');
    return ok(found);
  }),
  http.patch('*/api/v1/sessions/:id', async ({ request, params }) => {
    const id = params['id'] as string;
    const body = (await request.json().catch(() => ({}))) as Partial<SessionDetail>;
    requestLog.push({ method: 'PATCH', url: request.url, body });
    const base = SESSIONS_FIXTURE.find((s) => s.id === id) ?? makeSession({ id });
    return ok({ ...base, ...body, updated_at: '2026-10-18T03:00:00+00:00' });
  }),
  http.delete('*/api/v1/sessions/:id', ({ request, params }) => {
    requestLog.push({ method: 'DELETE', url: request.url });
    const id = params['id'] as string;
    if (!SESSIONS_FIXTURE.some((s) => s.id === id) && id !== 'sess-new') {
      return errorOf(404, 'SESSION_NOT_FOUND', '会话不存在');
    }
    return new HttpResponse(null, { status: 204 });
  }),

  http.post('*/api/v1/sessions/:id/messages', async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
    requestLog.push({ method: 'POST', url: request.url, body });
    if (typeof body['content'] !== 'string' || body['content'].trim() === '') {
      return errorOf(422, 'MESSAGE_EMPTY', '消息内容不能为空');
    }
    return ok({ message_id: 'msg-ack-1', run_id: 'run-ack-1', event_seq: 2 });
  }),
  http.post('*/api/v1/runs/:id/stop', ({ request, params }) => {
    requestLog.push({ method: 'POST', url: request.url });
    return ok({ run_id: params['id'] as string, status: 'stopped' });
  }),

  http.get('*/api/v1/sessions/:id/costs', () => ok(sessionCostFixture)),
  http.get('*/api/v1/costs/summary', ({ request }) => {
    const dimension = new URL(request.url).searchParams.get('dimension');
    if (dimension === 'date') return ok(costByDateFixture);
    if (dimension === 'model') return ok(costByModelFixture);
    if (dimension === 'session') return ok(costByDateFixture);
    return errorOf(422, 'PARAM_INVALID', 'dimension 必填');
  }),
  http.get('*/api/v1/sessions/:id/sandbox', () => ok(sandboxStatusFixture)),

  // SSE stub：App 壳冒烟用（空流立即关闭；流式行为测试一律 fake transport）。
  // msw v3 的 handler 返回面收敛了 HttpResponse 泛型——空体用 new HttpResponse(null)。
  http.get('*/api/v1/sessions/:id/events', () => {
    return new HttpResponse(null, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    }) as HttpResponse<string>;
  }),
];
