// @vitest-environment jsdom
/**
 * RunErrorBanner（B2 / M2 收口 S4；AC-004-4 失败如实呈现）：
 * - run 终态 error（store 收敛 idle 且 activeRun.error 保留）→ 横幅出现：
 *   role=alert + message 正文 + code 徽标 + retryable=true 时「可重试」提示；
 * - 纯 store 驱动无本地持久：新 run started / 后续终态事件（finished/stopped 防御路径）
 *   使 error 归 null 后，横幅自动消失；error 已收敛 idle 时 markRunEnded 幂等不清 error
 *   （store 既有语义，横幅保留）；
 * - idle 无 error 时不渲染任何节点。
 */
import { act, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { useChatStore } from '../store';
import { RunErrorBanner } from './RunErrorBanner';

import '../../../tests/test-utils';

function seedRunError(
  error: { code: 'model_error' | 'rate_limited' | 'internal'; message: string; retryable?: boolean | null } = {
    code: 'rate_limited',
    message: '模型提供方限流，请稍后重试',
    retryable: true,
  },
): void {
  act(() => {
    useChatStore.getState().applyRunStatus({
      subject: 'run',
      phase: 'started',
      runId: 'run-err-1',
      createdAt: '2026-10-18T00:00:00+00:00',
    });
    useChatStore.getState().applyRunStatus({
      subject: 'run',
      phase: 'error',
      runId: 'run-err-1',
      createdAt: '2026-10-18T00:00:05+00:00',
      error,
    });
  });
}

describe('RunErrorBanner · run 错误横幅（B2）', () => {
  it('idle 无 error：不渲染任何节点（也不出现 role=alert）', () => {
    render(<RunErrorBanner />);
    expect(screen.queryByTestId('run-error-banner')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('error 终态：横幅出现，role=alert，显示 message 与 code', () => {
    seedRunError({ code: 'model_error', message: '上游模型调用失败', retryable: false });
    render(<RunErrorBanner />);

    const banner = screen.getByTestId('run-error-banner');
    expect(banner).toBeInTheDocument();
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByTestId('run-error-message')).toHaveTextContent('上游模型调用失败');
    expect(screen.getByTestId('run-error-code')).toHaveTextContent('model_error');
    // store 已收敛 idle（error 与 finished/stopped 同为终态）
    expect(useChatStore.getState().activeRun.state).toBe('idle');
  });

  it('retryable=true：给出「可重试」提示；retryable 非 true：不出现该提示', () => {
    seedRunError({ code: 'rate_limited', message: '限流', retryable: true });
    const { unmount } = render(<RunErrorBanner />);
    expect(screen.getByTestId('run-error-retryable')).toHaveTextContent('可重试');
    unmount();

    seedRunError({ code: 'internal', message: '内部错误', retryable: false });
    render(<RunErrorBanner />);
    expect(screen.queryByTestId('run-error-retryable')).not.toBeInTheDocument();
  });

  it('新 run 开始（started）：横幅自动消失（store 驱动，无本地持久）', () => {
    seedRunError();
    render(<RunErrorBanner />);
    expect(screen.getByTestId('run-error-banner')).toBeInTheDocument();

    act(() => {
      useChatStore.getState().applyRunStatus({
        subject: 'run',
        phase: 'started',
        runId: 'run-new',
        createdAt: '2026-10-18T00:01:00+00:00',
      });
    });
    expect(screen.queryByTestId('run-error-banner')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('后续终态事件到达（finished 防御路径）：error 被覆盖为 null，横幅消失', () => {
    seedRunError();
    render(<RunErrorBanner />);
    expect(screen.getByTestId('run-error-banner')).toBeInTheDocument();

    // store 既有语义：error 收敛后已是 idle，markRunEnded 幂等不清 error（横幅保留）；
    // 迟到/防御性的 finished 终态才把 error 覆盖为 null
    act(() => {
      useChatStore.getState().markRunEnded();
    });
    expect(screen.getByTestId('run-error-banner')).toBeInTheDocument();

    act(() => {
      useChatStore.getState().applyRunStatus({
        subject: 'run',
        phase: 'finished',
        runId: 'run-err-1',
        createdAt: '2026-10-18T00:02:00+00:00',
      });
    });
    expect(screen.queryByTestId('run-error-banner')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(useChatStore.getState().activeRun.error).toBeNull();
  });
});
