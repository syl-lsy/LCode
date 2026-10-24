// @vitest-environment jsdom
/**
 * DecisionForm（AC-006-1~4；approval-card.html 四决策 + 第 5 档反馈框）：
 * - 四决策各自提交体（approve once/session、edit 校验、reject 附言可选）；
 * - respond 反馈必填；deciding 提交中禁用全部控件；ApiError 就地展示不裸抛；
 * - parseEditedArgs 纯函数：JSON 失败/数组/null/标量逐一报错。
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';

import { useApprovalStore } from '../store';
import { pendingApprovalA } from '../../../tests/msw-handlers';
import { server } from '../../../tests/test-utils';
import '../../../tests/test-utils';

import { DecisionForm, parseEditedArgs } from './DecisionForm';

const APPROVAL_ID = 'aprv-form-1';
const ARGS = { command: 'mkdir -p /root/obs-demo', timeout: 60 };

/** 决策提交的捕获器：记录 POST body 并回契约 ApprovalDecisionAck 形状。 */
function captureDecide(respond?: (body: Record<string, unknown>) => Response): {
  bodies: Record<string, unknown>[];
} {
  const bodies: Record<string, unknown>[] = [];
  server.use(
    http.post('*/api/v1/approvals/:id/decide', async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      bodies.push(body);
      if (respond !== undefined) return respond(body);
      return HttpResponse.json({
        code: '0',
        message: null,
        data: {
          approval: { ...pendingApprovalA, id: APPROVAL_ID, ...body, status: 'approved' },
          run_id: 'run-1',
        },
      });
    }),
  );
  return { bodies };
}

function renderForm(): ReturnType<typeof render> {
  return render(<DecisionForm approvalId={APPROVAL_ID} args={ARGS} />);
}

/** 预置挂起条目（组件本地 applyResolved 需要既有条目；真实链路由 approval_request 建立）。 */
beforeEach(() => {
  useApprovalStore.getState().upsertPending({
    id: APPROVAL_ID,
    sessionId: 'sess-1',
    tool: 'execute',
    args: ARGS,
    riskLevel: 'high',
    reason: null,
    createdAt: '2026-10-18T00:00:00+00:00',
    toolCallId: 'call_1',
    runId: 'run-1',
  });
});

/** userEvent.type 会把 [ / { 当按键语法，textarea 整段替换走 fireEvent.change。 */
function setEditorValue(value: string): void {
  fireEvent.change(screen.getByTestId('decision-args-editor'), { target: { value } });
}

describe('parseEditedArgs · edit 参数校验（就地报错不抛异常）', () => {
  it('合法对象解析通过；JSON 语法错误 / 数组 / null / 标量 / 空串逐一报错', () => {
    expect(parseEditedArgs('{"command":"ls"}')).toEqual({ ok: true, args: { command: 'ls' } });
    expect(parseEditedArgs('  {"a":1}  ').ok).toBe(true);

    for (const bad of ['{command:ls}', '[1,2]', 'null', '42', '"str"', 'true', '']) {
      const result = parseEditedArgs(bad);
      expect(result.ok).toBe(false);
      if (!result.ok) expect(result.error.length).toBeGreaterThan(0);
    }
  });
});

describe('DecisionForm · 变体感知选项文案（approval-card.html §8/§9 逐字）', () => {
  it('Bash 变体（缺省）：第 2 档「始终允许此命令」+ 原型说明；第 1/4 档逐字一致', () => {
    renderForm();
    const session = screen.getByTestId('decision-option-approve-session');
    expect(session).toHaveTextContent('始终允许此命令');
    expect(session).toHaveTextContent('项目范围内，后续相同命令不再询问');
    expect(screen.getByTestId('decision-option-approve-once')).toHaveTextContent('仅允许这一次');
    expect(screen.getByTestId('decision-option-reject')).toHaveTextContent('这次先拒绝');
    // 反馈框占位与确认行提示（原型逐字）
    expect(screen.getByTestId('decision-feedback')).toHaveAttribute(
      'placeholder',
      '告诉模型接下来应该怎么做...',
    );
    expect(screen.getByTestId('decision-form')).toHaveTextContent('使用 Tab / 上下键选择，回车确认');
  });

  it('文件变体（variant="file"）：第 2 档「始终允许本项目」+ 原型说明', () => {
    render(<DecisionForm approvalId={APPROVAL_ID} args={ARGS} variant="file" />);
    const session = screen.getByTestId('decision-option-approve-session');
    expect(session).toHaveTextContent('始终允许本项目');
    expect(session).toHaveTextContent('后续相同文件操作不再询问');
  });

  it('五个选项顺序稳定（契约 edit/respond 保位），选中态 aria-pressed 同步', async () => {
    const user = userEvent.setup();
    renderForm();
    const group = screen.getByRole('group', { name: '决策选项' });
    expect(Array.from(group.querySelectorAll('button')).map((b) => b.dataset.testid)).toEqual([
      'decision-option-approve-once',
      'decision-option-approve-session',
      'decision-option-edit',
      'decision-option-reject',
      'decision-option-respond',
    ]);

    await user.click(screen.getByTestId('decision-option-reject'));
    expect(screen.getByTestId('decision-option-reject')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('decision-option-approve-once')).toHaveAttribute('aria-pressed', 'false');
  });
});

describe('DecisionForm · 四决策提交体', () => {
  it('允许（once）：提交 {decision:approve, scope:once}；成功后本地落终态', async () => {
    const user = userEvent.setup();
    const { bodies } = captureDecide();
    renderForm();

    await user.click(screen.getByTestId('decision-option-approve-once'));
    await user.click(screen.getByTestId('decision-submit'));

    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({ decision: 'approve', scope: 'once' });
    await waitFor(() => expect(useApprovalStore.getState().byId.get(APPROVAL_ID)?.status).toBe('approved'));
  });

  it('本会话内始终允许：提交 {decision:approve, scope:session}', async () => {
    const user = userEvent.setup();
    const { bodies } = captureDecide();
    renderForm();

    await user.click(screen.getByTestId('decision-option-approve-session'));
    await user.click(screen.getByTestId('decision-submit'));

    await waitFor(() => expect(bodies).toEqual([{ decision: 'approve', scope: 'session' }]));
  });

  it('edit：编辑器初值为 args 原文；改参后提交 edited_args；非法 JSON 就地报错且不发请求', async () => {
    const user = userEvent.setup();
    const { bodies } = captureDecide();
    renderForm();

    await user.click(screen.getByTestId('decision-option-edit'));
    const editor = screen.getByTestId('decision-args-editor');
    expect(editor).toHaveValue(JSON.stringify(ARGS, null, 2));

    // 先提交非法 JSON → 就地报错、无请求
    await user.clear(editor);
    setEditorValue('[1,2]');
    await user.click(screen.getByTestId('decision-submit'));
    expect(screen.getByTestId('decision-args-error')).toHaveTextContent('数组');
    expect(bodies).toHaveLength(0);

    // 修正为合法对象 → edited_args 提交
    setEditorValue('{"command":"rm -rf dist/"}');
    await user.click(screen.getByTestId('decision-submit'));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({ decision: 'edit', edited_args: { command: 'rm -rf dist/' } });
  });

  it('reject：无附言提交 {decision:reject}；填附言后随 feedback 提交', async () => {
    const user = userEvent.setup();
    const { bodies } = captureDecide();
    renderForm();

    await user.click(screen.getByTestId('decision-option-reject'));
    await user.click(screen.getByTestId('decision-submit'));
    await waitFor(() => expect(bodies).toEqual([{ decision: 'reject' }]));

    await user.type(screen.getByTestId('decision-feedback'), '先不要删目录');
    await user.click(screen.getByTestId('decision-option-reject'));
    await user.click(screen.getByTestId('decision-submit'));
    await waitFor(() => expect(bodies).toHaveLength(2));
    expect(bodies[1]).toEqual({ decision: 'reject', feedback: '先不要删目录' });
  });

  it('respond：反馈必填——空反馈就地报错不发请求；填写后提交 feedback', async () => {
    const user = userEvent.setup();
    const { bodies } = captureDecide();
    renderForm();

    await user.click(screen.getByTestId('decision-option-respond'));
    await user.click(screen.getByTestId('decision-submit'));
    expect(screen.getByTestId('decision-error')).toHaveTextContent('反馈不能为空');
    expect(bodies).toHaveLength(0);

    await user.type(screen.getByTestId('decision-feedback'), '跳过建目录，直接创建文件');
    await user.click(screen.getByTestId('decision-submit'));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({ decision: 'respond', feedback: '跳过建目录，直接创建文件' });
  });

  it('未选决策直接确认 → decision-error 提示，不发请求', async () => {
    const user = userEvent.setup();
    const { bodies } = captureDecide();
    renderForm();

    await user.click(screen.getByTestId('decision-submit'));
    expect(screen.getByTestId('decision-error')).toHaveTextContent('请先选择一个决策');
    expect(bodies).toHaveLength(0);
  });
});

describe('DecisionForm · 提交中禁用与错误呈现', () => {
  it('deciding：全部控件禁用 + 确认钮「提交中…」；响应到达后恢复', async () => {
    const user = userEvent.setup();
    let resolveDecide: (() => void) | undefined;
    server.use(
      http.post('*/api/v1/approvals/:id/decide', async () => {
        await new Promise<void>((resolve) => {
          resolveDecide = resolve;
        });
        return HttpResponse.json({
          code: '0',
          message: null,
          data: { approval: { ...pendingApprovalA, id: APPROVAL_ID, status: 'approved' }, run_id: 'run-1' },
        });
      }),
    );
    renderForm();

    await user.click(screen.getByTestId('decision-option-approve-once'));
    await user.click(screen.getByTestId('decision-submit'));

    await waitFor(() => expect(screen.getByTestId('decision-submit')).toHaveTextContent('提交中…'));
    expect(screen.getByTestId('decision-submit')).toBeDisabled();
    expect(screen.getByTestId('decision-option-approve-once')).toBeDisabled();
    expect(screen.getByTestId('decision-option-approve-session')).toBeDisabled();
    expect(screen.getByTestId('decision-option-edit')).toBeDisabled();
    expect(screen.getByTestId('decision-option-reject')).toBeDisabled();
    expect(screen.getByTestId('decision-option-respond')).toBeDisabled();
    expect(screen.getByTestId('decision-feedback')).toBeDisabled();

    await waitFor(() => expect(resolveDecide).toBeDefined());
    await act(async () => {
      resolveDecide?.();
    });
    await waitFor(() => expect(screen.getByTestId('decision-submit')).toBeEnabled());
    expect(screen.getByTestId('decision-submit')).toHaveTextContent('确认');
  });

  it('409 APPROVAL_ALREADY_RESOLVED → 就地展示「决策提交失败（code）：message」，不裸抛', async () => {
    const user = userEvent.setup();
    captureDecide(() =>
      HttpResponse.json(
        { code: 'APPROVAL_ALREADY_RESOLVED', message: '该审批已裁决' },
        { status: 409 },
      ),
    );
    renderForm();

    await user.click(screen.getByTestId('decision-option-approve-once'));
    await user.click(screen.getByTestId('decision-submit'));

    const errorEl = await screen.findByTestId('decision-error');
    expect(errorEl).toHaveTextContent('决策提交失败（APPROVAL_ALREADY_RESOLVED）：该审批已裁决');
  });

  it('网络失败（NETWORK_ERROR 合成码）同样就地展示', async () => {
    const user = userEvent.setup();
    server.use(
      http.post('*/api/v1/approvals/:id/decide', () => HttpResponse.error()),
    );
    renderForm();

    await user.click(screen.getByTestId('decision-option-reject'));
    await user.click(screen.getByTestId('decision-submit'));

    const errorEl = await screen.findByTestId('decision-error');
    expect(errorEl.textContent).toMatch(/^决策提交失败（NETWORK_ERROR）：/);
  });
});
