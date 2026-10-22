/**
 * 端点调用面单测（A4 §7.2；契约 ENDPOINTS 路径/方法/载荷对齐）。
 *
 * 手法：注入 fake fetch 记录请求（url/method/body/query），错误信封短路 → 断言
 * ApiError 透传；成功路径用契约 fixtures（session_detail / message_send_ack /
 * cost_summary）证明 zod 解包链路。SSE 端点归 events/sse.ts，不在本调用面。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from './http';
import { decideApproval, getApproval, listApprovals } from './approvals';
import { getCostSummary, getSessionCost } from './costs';
import { listMessages, sendMessage, stopRun } from './messages';
import { listModels } from './models';
import { getSandboxStatus } from './sandbox';
import { createSession, deleteSession, getSession, listSessions, patchSession } from './sessions';

import approvalItemAuto from '@contracts-fixtures/rest/valid/approval_item_auto.json';
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

describe('approvals.ts 端点面（M3 Phase 2）', () => {
  it('listApprovals：GET /approvals（status/session_id 查询串透传；恢复挂起用）', async () => {
    const spy = install(() => errorEnvelope());

    await listApprovals().catch(() => undefined);
    expect(spy.calls[0]?.url).toBe('/api/v1/approvals');

    await listApprovals({ status: 'pending', sessionId: 'sess-a' }).catch(() => undefined);
    expect(spy.calls[1]?.url).toBe('/api/v1/approvals?status=pending&session_id=sess-a');
    spy.restore();
  });

  it('getApproval：GET /approvals/{id}（id 编码；404 APPROVAL_NOT_FOUND 经 ApiError 透传）', async () => {
    const spy = install(() =>
      new Response(JSON.stringify({ code: 'APPROVAL_NOT_FOUND', message: '审批不存在' }), {
        status: 404,
        headers: { 'content-type': 'application/json' },
      }),
    );

    const err = await getApproval('aprv x').catch((e: unknown) => e);
    expect(spy.calls[0]?.url).toBe(`/api/v1/approvals/${encodeURIComponent('aprv x')}`);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe('APPROVAL_NOT_FOUND');
    spy.restore();
  });

  it('decideApproval：POST /approvals/{id}/decide（四决策体透传 + ApprovalDecisionAck 解包）', async () => {
    const spy = install(() => okEnvelope({ approval: approvalItemAuto, run_id: 'run-1' }));

    const ack = await decideApproval('aprv-auto', { decision: 'approve', scope: 'once' });
    expect(spy.calls[0]).toMatchObject({
      url: '/api/v1/approvals/aprv-auto/decide',
      init: { method: 'POST' },
    });
    expect(spy.calls[0]?.init.body).toBe(JSON.stringify({ decision: 'approve', scope: 'once' }));
    expect(ack.approval.status).toBe('auto_approved');
    expect(ack.run_id).toBe('run-1');
    spy.restore();

    // edit 决策体：edited_args 原样序列化
    const spy2 = install(() => errorEnvelope());
    const edited = { command: 'rm -rf dist/', timeout: 120 };
    await decideApproval('aprv-1', { decision: 'edit', edited_args: edited }).catch(() => undefined);
    expect(spy2.calls[0]?.init.body).toBe(
      JSON.stringify({ decision: 'edit', edited_args: edited }),
    );
    spy2.restore();
  });

  it('listApprovals 成功路径：ApprovalPage 走契约 schema 解包（fixture 同源）', async () => {
    const spy = install(() => okEnvelope({ items: [approvalItemAuto] }));

    const page = await listApprovals({ status: 'auto_approved' });
    expect(page.items).toEqual([approvalItemAuto]);
    expect(page.items[0]?.status).toBe('auto_approved'); // AC-013-3 审计面恢复
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
