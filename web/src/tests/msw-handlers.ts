/**
 * REST mock handlers（A4 §10.3）：按端点表组织（A4 §7.2），信封按契约双信封格式
 * `{code:"0",message,data}`。SSE 不走 MSW——events 端点仅提供「立即关闭的空流」stub
 * 供 App 壳冒烟（流式行为一律走 fake transport，A4 §10.1）。
 *
 * fixtures 供测试断言/覆盖：`overrideHandlers()` 局部替换（server.use）。
 */

import { http, HttpResponse } from 'msw';

import type {
  ApprovalItem,
  ApprovalPage,
  CostSummary,
  DiffFileDetail,
  DiffReviewDetail,
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

/** 测试审批行（工厂：字段与契约 ApprovalItem 逐值对应，zod 形状同源）。 */
export function makeApproval(overrides: Partial<ApprovalItem> = {}): ApprovalItem {
  return {
    id: 'aprv-1',
    session_id: 'sess-1',
    tool: 'execute',
    args: { command: 'mkdir -p /root/obs-demo && ls -la /root/obs-demo' },
    risk_level: 'high',
    reason: '命中危险命令规则',
    created_at: '2026-10-18T00:00:00+00:00',
    status: 'pending',
    ...overrides,
  };
}

/** 挂起审批（sess-a 恢复场景默认挂两条：批次守门/多挂起测试共用）。 */
export const pendingApprovalA = makeApproval({
  id: 'aprv-a1',
  session_id: 'sess-a',
  tool: 'execute',
  args: { command: 'rm -rf build/', timeout: 120 },
  risk_level: 'high',
  reason: '命中危险命令规则：rm -rf',
});
export const pendingApprovalB = makeApproval({
  id: 'aprv-a2',
  session_id: 'sess-a',
  tool: 'write_file',
  args: { path: 'hello.py', content: 'print(1)' },
  risk_level: 'medium',
  reason: '文件写入需要确认',
});
/** AUTO 分类器留痕行（auto_approved 形状来自契约 fixture 同构）。 */
export const autoApprovedApproval = makeApproval({
  id: 'aprv-auto',
  session_id: 'sess-a',
  tool: 'write_file',
  args: { path: 'src/app.py', content: "print('hello')" },
  risk_level: 'medium',
  status: 'auto_approved',
  decision: 'approve',
  scope: 'once',
  decided_at: '2026-10-18T00:00:00.200000+00:00',
  reason: '分类器：常规非沙箱写操作，命中 AUTO 规则集（低风险路径）',
});

/** 全量审批表（GET /approvals 过滤的数据源；测试可 override）。 */
export let APPROVALS_FIXTURE: ApprovalItem[] = [
  pendingApprovalA,
  pendingApprovalB,
  autoApprovedApproval,
];

/** 测试内替换审批表（server.resetHandlers 后由 fixtures 基准重建）。 */
export function setApprovalsFixture(items: readonly ApprovalItem[]): void {
  APPROVALS_FIXTURE = [...items];
}

// ---------------------------------------------------------------- diff reviews

/** GET …/files/{file_path} 的路由匹配正则（file_path 含 `/`，`:param` 单段通配不到）。 */
export const DIFF_FILES_ROUTE = /files/;
/** 从 pathname 解析 (reviewId, filePath) 的正则（handler 与测试共用）。 */
const DIFF_FILE_PATH_RE = /\/api\/v1\/diff-reviews\/([^/]+)\/files\/(.+)$/;

const PATCH_SAMPLE = [
  'diff --git a/app.py b/app.py',
  'index 64d908b..6912166 100644',
  '--- a/app.py',
  '+++ b/app.py',
  '@@ -1,3 +1,4 @@',
  ' import os',
  '+import sys',
  ' ',
  ' def main():',
  '-    pass',
  '+    return 0',
  '',
].join('\n');

/** 测试 diff 审查行（工厂：字段与契约 DiffReviewDetail 逐值对应，zod 形状同源）。 */
export function makeDiffReview(overrides: Partial<DiffReviewDetail> = {}): DiffReviewDetail {
  return {
    id: '0192f0a1-6666-4a1a-9c2d-000000000006',
    session_id: 'sess-1',
    run_id: null,
    round: 1,
    verdict: 'pending',
    baseline: {
      kind: 'init',
      ref: 'lcode-baseline/round-0',
      commit: 'e83c5163316f89b8de6d78a1e58a28f6a3d1a1d0',
    },
    files: [
      {
        path: 'app.py',
        old_path: null,
        change: 'modified',
        additions: 5,
        deletions: 2,
        binary: false,
      },
    ],
    additions_total: 5,
    deletions_total: 2,
    feedback: null,
    decided_by: null,
    created_at: '2026-10-18T00:00:00+00:00',
    resolved_at: null,
    ...overrides,
  };
}

/** 测试单文件 patch 详情（契约 DiffFileDetail）。 */
export function makeDiffFile(overrides: Partial<DiffFileDetail> = {}): DiffFileDetail {
  return {
    path: 'app.py',
    change: 'modified',
    additions: 5,
    deletions: 2,
    binary: false,
    patch: PATCH_SAMPLE,
    blob_ref: null,
    ...overrides,
  };
}

/** 全量审查表（GET /diff-reviews 数据源；测试用 setDiffReviewsFixture 替换）。 */
export let DIFF_REVIEWS_FIXTURE: DiffReviewDetail[] = [];

export function setDiffReviewsFixture(items: readonly DiffReviewDetail[]): void {
  DIFF_REVIEWS_FIXTURE = [...items];
}

/** 逐审查的文件详情表（GET …/files 数据源；键 = reviewId）。 */
export let DIFF_FILES_FIXTURE: Readonly<Record<string, readonly DiffFileDetail[]>> = {};

export function setDiffFilesFixture(reviewId: string, items: readonly DiffFileDetail[]): void {
  DIFF_FILES_FIXTURE = { ...DIFF_FILES_FIXTURE, [reviewId]: items };
}

/** decision → 落库后 status（与 contracts rest.py ApprovalItem.status 对应）。 */
type ApprovalDecision = NonNullable<ApprovalItem['decision']>;

const DECIDE_STATUS: Record<ApprovalDecision, ApprovalItem['status']> = {
  approve: 'approved',
  edit: 'edited',
  reject: 'rejected',
  respond: 'responded',
};

function isApprovalDecision(value: unknown): value is ApprovalDecision {
  return typeof value === 'string' && value in DECIDE_STATUS;
}

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

  // ---- approvals（M3 Phase 2：恢复挂起 + 四决策；响应形状符合契约 zod）----
  http.get('*/api/v1/approvals', ({ request }) => {
    const url = new URL(request.url);
    const status = url.searchParams.get('status');
    const sessionId = url.searchParams.get('session_id');
    const items = APPROVALS_FIXTURE.filter(
      (item) =>
        (status === null || item.status === status) &&
        (sessionId === null || item.session_id === sessionId),
    );
    const page: ApprovalPage = { items };
    return ok(page);
  }),
  http.get('*/api/v1/approvals/:id', ({ params }) => {
    const id = params['id'] as string;
    const found = APPROVALS_FIXTURE.find((item) => item.id === id);
    if (found === undefined) return errorOf(404, 'APPROVAL_NOT_FOUND', '审批不存在');
    return ok(found);
  }),
  http.post('*/api/v1/approvals/:id/decide', async ({ request, params }) => {
    const id = params['id'] as string;
    const body = (await request.json().catch(() => ({}))) as {
      decision?: string;
      scope?: string | null;
      edited_args?: Record<string, unknown> | null;
      feedback?: string | null;
    };
    requestLog.push({ method: 'POST', url: request.url, body });
    const found = APPROVALS_FIXTURE.find((item) => item.id === id);
    if (found === undefined) return errorOf(404, 'APPROVAL_NOT_FOUND', '审批不存在');
    if (found.status !== 'pending') {
      return errorOf(409, 'APPROVAL_ALREADY_RESOLVED', '该审批已裁决');
    }
    if (!isApprovalDecision(body.decision)) {
      return errorOf(422, 'PARAM_INVALID', 'decision 必须是 approve/edit/reject/respond');
    }
    // 就地落库（仅影响本测试进程的 fixture 副本），返回契约 ApprovalDecisionAck 形状
    const decision = body.decision;
    const decided: ApprovalItem = {
      ...found,
      status: DECIDE_STATUS[decision],
      decision,
      scope: (body.scope as ApprovalItem['scope'] | undefined) ?? 'once',
      decided_at: '2026-10-18T00:00:10+00:00',
    };
    APPROVALS_FIXTURE = APPROVALS_FIXTURE.map((item) => (item.id === id ? decided : item));
    return ok({ approval: decided, run_id: 'run-1' });
  }),

  // ---- diff reviews（M3 Phase 4：审查面板；响应形状符合契约 zod）----
  // files 路由不能用 `:param`（file_path 含 `/`，单段通配匹配不到），改用正则 +
  // handler 内自行从 pathname 解析（DIFF_FILES_ROUTE 供测试 override 复用）。
  http.get('*/api/v1/diff-reviews/:id', ({ request, params }) => {
    const id = params['id'] as string;
    requestLog.push({ method: 'GET', url: request.url });
    const found = DIFF_REVIEWS_FIXTURE.find((item) => item.id === id);
    if (found === undefined) return errorOf(404, 'DIFF_REVIEW_NOT_FOUND', '差异审查不存在');
    return ok(found);
  }),
  http.get(DIFF_FILES_ROUTE, ({ request }) => {
    const match = DIFF_FILE_PATH_RE.exec(new URL(request.url).pathname);
    requestLog.push({ method: 'GET', url: request.url });
    if (match === null) return errorOf(404, 'NOT_FOUND', '路径不合法');
    const reviewId = decodeURIComponent(match[1] ?? '');
    const filePath = decodeURIComponent(match[2] ?? '');
    const review = DIFF_REVIEWS_FIXTURE.find((item) => item.id === reviewId);
    if (review === undefined) return errorOf(404, 'DIFF_REVIEW_NOT_FOUND', '差异审查不存在');
    const found = (DIFF_FILES_FIXTURE[reviewId] ?? []).find((file) => file.path === filePath);
    if (found === undefined) return errorOf(404, 'NOT_FOUND', '文件不在该轮 diff 中');
    return ok(found);
  }),
  http.post('*/api/v1/diff-reviews/:id/verdict', async ({ request, params }) => {
    const id = params['id'] as string;
    const body = (await request.json().catch(() => ({}))) as {
      verdict?: string;
      feedback?: string | null;
    };
    requestLog.push({ method: 'POST', url: request.url, body });
    const found = DIFF_REVIEWS_FIXTURE.find((item) => item.id === id);
    if (found === undefined) return errorOf(404, 'DIFF_REVIEW_NOT_FOUND', '差异审查不存在');
    if (found.verdict !== 'pending') {
      return errorOf(409, 'DIFF_ALREADY_RESOLVED', '该差异审查已裁决');
    }
    if (body.verdict !== 'accepted' && body.verdict !== 'rejected') {
      return errorOf(422, 'INVALID_ARGUMENT', 'verdict 必须是 accepted/rejected');
    }
    // 就地落库（仅影响本测试进程的 fixture 副本），返回契约 DiffReviewDetail 形状
    const decided: DiffReviewDetail = {
      ...found,
      verdict: body.verdict,
      feedback: body.verdict === 'rejected' ? (body.feedback ?? null) : null,
      decided_by: 'user',
      resolved_at: '2026-10-18T00:00:10+00:00',
    };
    DIFF_REVIEWS_FIXTURE = DIFF_REVIEWS_FIXTURE.map((item) => (item.id === id ? decided : item));
    return ok(decided);
  }),

  // SSE stub：App 壳冒烟用（空流立即关闭；流式行为测试一律 fake transport）。
  // msw v3 的 handler 返回面收敛了 HttpResponse 泛型——空体用 new HttpResponse(null)。
  http.get('*/api/v1/sessions/:id/events', () => {
    return new HttpResponse(null, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    }) as HttpResponse<string>;
  }),
];
