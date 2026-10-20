// @vitest-environment jsdom
/**
 * Composer（AC-001-1/2/4）：
 * - 空输入不发请求 + 轻提示（AC-001-4）；
 * - 正常发送：POST /messages + onSent(ack)（水位交给 App 接 ensureCaughtUp）；
 * - 运行中 = 停止生成（幂等 POST /runs/{id}/stop），已收内容不被清空；
 * - 等待审批期间发送禁用（审批唯一闸门，R9）。
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { http, HttpResponse } from 'msw';

import { useChatStore } from './store';
import { Composer } from './Composer';
import { requestLog } from '../../tests/msw-handlers';
import { server } from '../../tests/test-utils';

const SESSION = 'sess-a';

function seedRunning(runId = 'run-1'): void {
  useChatStore.getState().applyRunStatus({
    subject: 'run',
    phase: 'started',
    runId,
    createdAt: '2026-10-18T00:00:00+00:00',
  });
}

function seedRunError(runId = 'run-err'): void {
  seedRunning(runId);
  useChatStore.getState().applyRunStatus({
    subject: 'run',
    phase: 'error',
    runId,
    createdAt: '2026-10-18T00:00:05+00:00',
    error: { code: 'rate_limited', message: '限流', retryable: true },
  });
}

describe('Composer · 发送与停止（AC-001-1/2/4）', () => {
  it('空输入不发请求：Enter 提交被拦截并给轻提示（MESSAGE_EMPTY 不出网）', async () => {
    const user = userEvent.setup();
    requestLog.length = 0;
    render(<Composer sessionId={SESSION} />);

    // 空输入时发送钮禁用（原型口径）；轻提示走 Enter 提交路径
    expect(screen.getByTestId('composer-send')).toBeDisabled();
    await user.type(screen.getByTestId('composer-input'), '{Enter}');
    expect(screen.getByTestId('composer-hint')).toHaveTextContent('请输入内容后再发送');
    expect(requestLog.filter((entry) => entry.url.includes('/messages'))).toHaveLength(0);
  });

  it('纯空白输入同样不发请求（trim 语义与契约一致）', async () => {
    const user = userEvent.setup();
    requestLog.length = 0;
    render(<Composer sessionId={SESSION} />);
    await user.type(screen.getByTestId('composer-input'), '   ');
    await user.click(screen.getByTestId('composer-send'));
    expect(requestLog.filter((entry) => entry.url.includes('/messages'))).toHaveLength(0);
  });

  it('发送：POST /messages 载荷正确，onSent 收到 ack（event_seq 水位），输入清空', async () => {
    const user = userEvent.setup();
    const onSent = vi.fn();
    requestLog.length = 0;
    render(<Composer sessionId={SESSION} onSent={onSent} />);

    await user.type(screen.getByTestId('composer-input'), '帮我修复登录超时');
    await user.click(screen.getByTestId('composer-send'));

    await waitFor(() => {
      expect(onSent).toHaveBeenCalledWith({ message_id: 'msg-ack-1', run_id: 'run-ack-1', event_seq: 2 });
    });
    const post = requestLog.find((entry) => entry.url.includes('/messages'));
    expect(post?.body).toEqual({ content: '帮我修复登录超时' });
    expect(screen.getByTestId('composer-input')).toHaveValue('');
  });

  it('Enter 提交、Shift+Enter 换行（IME composing 保护不在本测覆盖）', async () => {
    const user = userEvent.setup();
    const onSent = vi.fn();
    render(<Composer sessionId={SESSION} onSent={onSent} />);
    const input = screen.getByTestId('composer-input');

    await user.type(input, '第一行{Shift>}{Enter}{/Shift}第二行');
    expect(input).toHaveValue('第一行\n第二行');
    expect(onSent).not.toHaveBeenCalled();

    await user.type(input, '{Enter}');
    await waitFor(() => expect(onSent).toHaveBeenCalledTimes(1));
  });

  it('RUN_ALREADY_ACTIVE：如实提示（服务端已运行中的幂等兜底）', async () => {
    const user = userEvent.setup();
    server.use(
      http.post('*/api/v1/sessions/:id/messages', () =>
        HttpResponse.json({ code: 'RUN_ALREADY_ACTIVE', message: '已有运行中的 run' }, { status: 409 }),
      ),
    );
    render(<Composer sessionId={SESSION} />);
    await user.type(screen.getByTestId('composer-input'), 'hi');
    await user.click(screen.getByTestId('composer-send'));
    await waitFor(() => {
      expect(screen.getByTestId('composer-hint')).toHaveTextContent('已有运行中的任务');
    });
  });

  it('运行中：发送钮换成「停止生成」，点击 POST /runs/{id}/stop（AC-001-2）', async () => {
    const user = userEvent.setup();
    requestLog.length = 0;
    seedRunning('run-42');
    render(<Composer sessionId={SESSION} />);

    expect(screen.queryByTestId('composer-send')).not.toBeInTheDocument();
    await user.click(screen.getByTestId('composer-stop'));
    await waitFor(() => {
      expect(requestLog.some((entry) => entry.url.includes('/runs/run-42/stop'))).toBe(true);
    });
    // 停止不清空已收内容（内容在 store；Composer 只是不清输入/不 reset）
    expect(useChatStore.getState().activeRun.state).toBe('running'); // 等 run 终态事件收敛
  });

  it('run error 终态：收敛 idle 后 Composer 回到可输入态（可打字、发送钮可用，B2）', async () => {
    const user = userEvent.setup();
    seedRunError('run-err-1');
    render(<Composer sessionId={SESSION} />);

    // error 与 finished/stopped 同为终态：停止键消失、发送键回来且未被禁用
    expect(screen.queryByTestId('composer-stop')).not.toBeInTheDocument();
    const input = screen.getByTestId('composer-input');
    await user.type(input, '换个模型再试一次');
    expect(input).toHaveValue('换个模型再试一次');
    expect(screen.getByTestId('composer-send')).toBeEnabled();
    expect(useChatStore.getState().activeRun.state).toBe('idle');
  });

  it('停止返回 RUN_NOT_ACTIVE：提示「该运行已结束」并收敛回可输入态，不误报停止失败（F12）', async () => {
    const user = userEvent.setup();
    server.use(
      http.post('*/api/v1/runs/:id/stop', () =>
        HttpResponse.json({ code: 'RUN_NOT_ACTIVE', message: '运行已结束' }, { status: 409 }),
      ),
    );
    seedRunning('run-42');
    render(<Composer sessionId={SESSION} />);

    await user.click(screen.getByTestId('composer-stop'));
    await waitFor(() => {
      expect(screen.getByTestId('composer-hint')).toHaveTextContent('该运行已结束');
    });
    // 回到可输入态：停止键消失、发送键回来、store 状态机归 idle
    expect(screen.queryByTestId('composer-stop')).not.toBeInTheDocument();
    expect(screen.getByTestId('composer-send')).toBeInTheDocument();
    expect(useChatStore.getState().activeRun.state).toBe('idle');
    // 不出现误报文案
    expect(screen.getByTestId('composer-hint')).not.toHaveTextContent('停止失败');
  });

  it('停止返回其他错误：仍如实提示「停止失败（code）」且不收敛运行态（F12 不吞真失败）', async () => {
    const user = userEvent.setup();
    server.use(
      http.post('*/api/v1/runs/:id/stop', () =>
        HttpResponse.json({ code: 'INTERNAL', message: '内部错误' }, { status: 500 }),
      ),
    );
    seedRunning('run-42');
    render(<Composer sessionId={SESSION} />);

    await user.click(screen.getByTestId('composer-stop'));
    await waitFor(() => {
      expect(screen.getByTestId('composer-hint')).toHaveTextContent('停止失败（INTERNAL）');
    });
    // 真失败不收敛：仍处运行态，等用户重试
    expect(screen.getByTestId('composer-stop')).toBeInTheDocument();
    expect(useChatStore.getState().activeRun.state).toBe('running');
  });

  it('等待审批：发送禁用并提示（审批窗口期 R9）', async () => {
    seedRunning('run-42');
    useChatStore.getState().noteApprovalPending();
    render(<Composer sessionId={SESSION} />);
    expect(screen.queryByTestId('composer-send')).not.toBeInTheDocument(); // 运行中 → 停止键
    expect(screen.getByTestId('composer-hint')).toHaveTextContent('等待审批处理');
  });

  it('sessionId 为空：走 onSendWithoutSession 兜底（首页建会话路径）', async () => {
    const user = userEvent.setup();
    const onSendWithoutSession = vi.fn().mockResolvedValue(undefined);
    render(<Composer sessionId={null} onSendWithoutSession={onSendWithoutSession} />);
    await user.type(screen.getByTestId('composer-input'), '新任务');
    await user.click(screen.getByTestId('composer-send'));
    await waitFor(() => {
      expect(onSendWithoutSession).toHaveBeenCalledWith('新任务');
    });
  });
});
