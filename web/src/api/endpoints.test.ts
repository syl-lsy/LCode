/**
 * 端点调用面单测（A4 §7.2；契约 ENDPOINTS 路径/方法/载荷对齐）。
 *
 * 手法：注入 fake fetch 记录请求（url/method/body/query），错误信封短路 → 断言
 * ApiError 透传；成功路径用契约 fixtures（session_detail / message_send_ack /
 * cost_summary）证明 zod 解包链路。SSE 端点归 events/sse.ts，不在本调用面。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from './http';
import { getCostSummary, getSessionCost } from './costs';
import { listMessages, sendMessage, stopRun } from './messages';
import { listModels } from './models';
import { getSandboxStatus } from './sandbox';
import { createSession, deleteSession, getSession, listSessions, patchSession } from './sessions';

import costSummaryFixture from '@contracts-fixtures/rest/valid/cost_summary.json';
import messageSendAck from '@contracts-fixtures/rest/valid/message_send_ack.json';
import sessionDetail from '@contracts-fixtures/rest/valid/session_detail.json';

interface RecordedCall {
  readonly url: string;
  readonly init: RequestInit;
}

function install(responder: () => Response): { calls: RecordedCall[]; restore(): void } {
  const calls: RecordedCall[] = [];
  const impl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), init: init ?? {} });
    return responder();
  }) as unknown as typeof fetch;
  const original = globalThis.fetch;
  globalThis.fetch = impl;
  return {
    calls,
    restore: () => {
      globalThis.fetch = original;
    },
  };
}

function errorEnvelope(): Response {
  return new Response(JSON.stringify({ code: 'INVALID_ARGUMENT', message: 'bad' }), {
    status: 422,
    headers: { 'content-type': 'application/json' },
  });
}

function okEnvelope(data: unknown): Response {
  return new Response(JSON.stringify({ code: '0', message: 'ok', data }), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

beforeEach(() => {
  vi.stubEnv('VITE_API_BASE_URL', '/api/v1');
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe('sessions.ts 端点面', () => {
  it('listSessions：GET /sessions（可带 title 子串搜索）', async () => {
    const spy = install(() => errorEnvelope());

    await expect(listSessions()).rejects.toBeInstanceOf(ApiError);
    expect(spy.calls[0]?.url).toBe('/api/v1/sessions');

    await expect(listSessions({ title: '登录' })).rejects.toBeInstanceOf(ApiError);
    expect(spy.calls[1]?.url).toBe(`/api/v1/sessions?title=${encodeURIComponent('登录')}`);
    expect(spy.calls[1]?.init.method).toBe('GET');
    spy.restore();
  });

  it('getSession / createSession / patchSession / deleteSession 的路径与方法', async () => {
    const spy = install(() => errorEnvelope());

    await getSession('sess 1').catch(() => undefined);
    expect(spy.calls.at(-1)?.url).toBe(`/api/v1/sessions/${encodeURIComponent('sess 1')}`);

    await createSession({ title: '新会话' }).catch(() => undefined);
    expect(spy.calls.at(-1)).toMatchObject({ url: '/api/v1/sessions', init: { method: 'POST' } });
    expect(spy.calls.at(-1)?.init.body).toBe(JSON.stringify({ title: '新会话' }));

    await patchSession('s1', { model: 'qwen3-max' }).catch(() => undefined);
    expect(spy.calls.at(-1)).toMatchObject({ url: '/api/v1/sessions/s1', init: { method: 'PATCH' } });
    expect(spy.calls.at(-1)?.init.body).toBe(JSON.stringify({ model: 'qwen3-max' }));

    // fake 响应器恒回错误信封：DELETE 断言只看方法/路径（204→undefined 语义由 http.test 覆盖）
    await expect(deleteSession('s1')).rejects.toBeInstanceOf(ApiError);
    expect(spy.calls.at(-1)).toMatchObject({ url: '/api/v1/sessions/s1', init: { method: 'DELETE' } });
    spy.restore();
  });

  it('成功路径：SessionDetail 走契约 schema 解包（fixture 同源）', async () => {
    const spy = install(() => okEnvelope(sessionDetail));

    const detail = await getSession(sessionDetail.id);
    expect(detail).toEqual(sessionDetail);
    expect(detail.run_state).toBe('waiting_approval'); // 刷新恢复种子的关键字段
    spy.restore();
  });
});

describe('messages.ts 端点面', () => {
  it('sendMessage：POST /sessions/{id}/messages（MessageSendAck 水位）', async () => {
    const spy = install(() => okEnvelope(messageSendAck));

    const ack = await sendMessage('s1', { content: '修复 bug' });
    expect(spy.calls[0]).toMatchObject({
      url: '/api/v1/sessions/s1/messages',
      init: { method: 'POST' },
    });
    expect(spy.calls[0]?.init.body).toBe(JSON.stringify({ content: '修复 bug' }));
    expect(ack).toEqual(messageSendAck); // event_seq 即订阅水位（§4.2 路径 B）
    spy.restore();
  });

  it('listMessages：游标分页（cursor 透传）', async () => {
    const spy = install(() => errorEnvelope());

    await listMessages('s1').catch(() => undefined);
    expect(spy.calls[0]?.url).toBe('/api/v1/sessions/s1/messages');

    await listMessages('s1', { cursor: 'cur_2' }).catch(() => undefined);
    expect(spy.calls[1]?.url).toBe('/api/v1/sessions/s1/messages?cursor=cur_2');
    spy.restore();
  });

  it('stopRun：POST /runs/{id}/stop（幂等，StopRunAck）', async () => {
    const spy = install(() => okEnvelope({ run_id: 'run-1', status: 'stopped' }));

    const ack = await stopRun('run-1');
    expect(spy.calls[0]).toMatchObject({ url: '/api/v1/runs/run-1/stop', init: { method: 'POST' } });
    expect(ack).toEqual({ run_id: 'run-1', status: 'stopped' });
    spy.restore();
  });
});

describe('models / costs / sandbox 端点面', () => {
  it('listModels：GET /models', async () => {
    const spy = install(() =>
      okEnvelope({
        default_model: 'qwen3-coder-plus',
        models: [],
      }),
    );

    const list = await listModels();
    expect(spy.calls[0]?.url).toBe('/api/v1/models');
    expect(list.default_model).toBe('qwen3-coder-plus');
    spy.restore();
  });

  it('getSessionCost：GET /sessions/{id}/costs（模型段 + 沙箱段）', async () => {
    const spy = install(() =>
      okEnvelope({
        model: { input_tokens: 100, output_tokens: 50 },
        model_cost: 0.5,
        sandbox: { accrued: 0.1, currency: 'CNY', rate_per_hour: 0.156 },
      }),
    );

    const cost = await getSessionCost('s1');
    expect(spy.calls[0]?.url).toBe('/api/v1/sessions/s1/costs');
    expect(cost.model_cost).toBe(0.5);
    expect(cost.sandbox.rate_per_hour).toBe(0.156);
    spy.restore();
  });

  it('getCostSummary：dimension 必填参数透传（AC-015-2 三维）', async () => {
    const spy = install(() => okEnvelope(costSummaryFixture));

    const summary = await getCostSummary('session');
    expect(spy.calls[0]?.url).toBe('/api/v1/costs/summary?dimension=session');
    expect(summary).toEqual(costSummaryFixture);
    spy.restore();
  });

  it('getSandboxStatus：GET /sessions/{id}/sandbox（AC-015-4）', async () => {
    const spy = install(() =>
      okEnvelope({
        phase: 'running',
        provider: 'bailian',
        sandbox_id: 'sbx_1',
        running_since: '2026-10-17T09:00:00+00:00',
        cost_estimate: { accrued: 0.31, currency: 'CNY', rate_per_hour: 0.156 },
      }),
    );

    const status = await getSandboxStatus('s1');
    expect(spy.calls[0]?.url).toBe('/api/v1/sessions/s1/sandbox');
    expect(status).toMatchObject({ phase: 'running', provider: 'bailian' });
    spy.restore();
  });
});
