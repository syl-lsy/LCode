// @vitest-environment jsdom
/**
 * ApprovalModeMenu（AC-013-3/4）：MANUAL/AUTO 切换 PATCH sessions（服务端回声
 * syncFromSession 同模式）、沙箱红线常驻提示、切换失败就地报错不静默回滚。
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';

import type { SessionDetail } from '@contracts/rest';

import { useSessionStore } from '../store';

import { makeSession, requestLog } from '../../../tests/msw-handlers';
import { server } from '../../../tests/test-utils';
import '../../../tests/test-utils';

import { ApprovalModeMenu, SANDBOX_REDLINE_HINT } from './ApprovalModeMenu';

const SESSION: SessionDetail = makeSession({ id: 'sess-mode', title: '模式切换会话' });

function seedSession(mode: SessionDetail['approval_mode'] = 'manual'): void {
  useSessionStore.getState().setList([{ ...SESSION, approval_mode: mode }]);
}

describe('ApprovalModeMenu · MANUAL/AUTO 切换（AC-013-3）', () => {
  it('默认显示当前模式（手动）；菜单含红线提示与生效时机说明', async () => {
    const user = userEvent.setup();
    seedSession('manual');
    render(<ApprovalModeMenu sessionId="sess-mode" />);

    expect(screen.getByTestId('approval-mode-menu')).toHaveTextContent('审批：手动');
    await user.click(screen.getByTestId('approval-mode-menu'));

    expect(screen.getByTestId('menu-item-manual')).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByTestId('menu-item-auto')).toHaveAttribute('aria-checked', 'false');
    // 沙箱红线（AC-013-4 固定文案）+ 时机说明常驻菜单内
    expect(screen.getByText(SANDBOX_REDLINE_HINT)).toBeInTheDocument();
    expect(screen.getByText(SANDBOX_REDLINE_HINT).closest('button')).toBeDisabled();
    expect(screen.getByText('切换自下一条消息生效')).toBeInTheDocument();
  });

  it('选「自动审批」→ PATCH approval_mode=auto，服务端回声落 store', async () => {
    const user = userEvent.setup();
    seedSession('manual');
    render(<ApprovalModeMenu sessionId="sess-mode" />);

    await user.click(screen.getByTestId('approval-mode-menu'));
    await user.click(screen.getByTestId('menu-item-auto'));

    await waitFor(() => {
      expect(useSessionStore.getState().list[0]?.approval_mode).toBe('auto');
    });
    const patch = requestLog.at(-1);
    expect(patch).toMatchObject({ method: 'PATCH', body: { approval_mode: 'auto' } });
  });

  it('AUTO 会话选回「手动审批」→ PATCH approval_mode=manual', async () => {
    const user = userEvent.setup();
    seedSession('auto');
    render(<ApprovalModeMenu sessionId="sess-mode" />);

    expect(screen.getByTestId('approval-mode-menu')).toHaveTextContent('审批：自动');
    await user.click(screen.getByTestId('approval-mode-menu'));
    await user.click(screen.getByTestId('menu-item-manual'));

    await waitFor(() => {
      expect(useSessionStore.getState().list[0]?.approval_mode).toBe('manual');
    });
  });

  it('PATCH 失败（非 2xx）→ 就地报错（code 透传），store 保持原模式不静默回滚', async () => {
    const user = userEvent.setup();
    server.use(
      http.patch('*/api/v1/sessions/:id', () =>
        HttpResponse.json({ code: 'RUN_ALREADY_ACTIVE', message: '运行中不允许切换' }, { status: 409 }),
      ),
    );
    seedSession('manual');
    render(<ApprovalModeMenu sessionId="sess-mode" />);

    await user.click(screen.getByTestId('approval-mode-menu'));
    await user.click(screen.getByTestId('menu-item-auto'));

    const errorEl = await screen.findByTestId('approval-mode-error');
    expect(errorEl).toHaveTextContent('审批模式切换失败（RUN_ALREADY_ACTIVE）：运行中不允许切换');
    expect(useSessionStore.getState().list[0]?.approval_mode).toBe('manual');
  });
});
