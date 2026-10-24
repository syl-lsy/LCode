// @vitest-environment jsdom
/**
 * ApprovalModeMenu（AC-013-3/4）：MANUAL/AUTO 切换 PATCH sessions（服务端回声
 * syncFromSession 同模式）、沙箱红线常驻提示、切换失败就地报错不静默回滚。
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { describe, expect, it, vi } from 'vitest';

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

    expect(screen.getByTestId('approval-mode-menu')).toHaveTextContent('审批：变更前确认');
    await user.click(screen.getByTestId('approval-mode-menu'));

    expect(screen.getByTestId('menu-item-manual')).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByTestId('menu-item-auto')).toHaveAttribute('aria-checked', 'false');
    // 沙箱红线（AC-013-4 固定文案）+ 时机说明常驻菜单内
    expect(screen.getByText(SANDBOX_REDLINE_HINT)).toBeInTheDocument();
    expect(screen.getByText(SANDBOX_REDLINE_HINT).closest('button')).toBeDisabled();
    expect(screen.getByText('切换自下一条消息生效')).toBeInTheDocument();
  });

  it('F6 四档呈现：计划模式（checkbox）/ 变更前确认 / 自主模式 / 完全访问；后两档 P0 契约外置灰', async () => {
    const user = userEvent.setup();
    seedSession('manual');
    render(<ApprovalModeMenu sessionId="sess-mode" />);

    await user.click(screen.getByTestId('approval-mode-menu'));
    // 四档文案逐字（toolbar-menus 菜单 2）
    expect(screen.getByTestId('menu-item-plan')).toHaveTextContent('计划模式');
    expect(screen.getByTestId('menu-item-manual')).toHaveTextContent('变更前确认');
    expect(screen.getByTestId('menu-item-auto')).toHaveTextContent('自主模式');
    expect(screen.getByTestId('menu-item-full')).toHaveTextContent('完全访问');
    // 计划模式与完全访问：P0 契约（MANUAL/AUTO 两档）之外 → 置灰占位
    expect(screen.getByTestId('menu-item-plan')).toBeDisabled();
    expect(screen.getByTestId('menu-item-full')).toBeDisabled();
    expect(screen.getByTestId('menu-item-manual')).toBeEnabled();
    expect(screen.getByTestId('menu-item-auto')).toBeEnabled();
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

    expect(screen.getByTestId('approval-mode-menu')).toHaveTextContent('审批：自主模式');
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

describe('ApprovalModeMenu · composer 变体（B1 原型还原：切换模式钮 + R11 pill）', () => {
  it('默认（manual）只显示「切换模式：手动」tool-btn，无 tb-sep 与 pill', async () => {
    const user = userEvent.setup();
    render(<ApprovalModeMenu sessionId={null} value="manual" onChange={() => undefined} variant="composer" />);

    expect(screen.getByTestId('composer-mode-menu')).toHaveTextContent('切换模式：变更前确认');
    expect(screen.queryByTestId('composer-mode-sep')).not.toBeInTheDocument();
    expect(screen.queryByTestId('composer-mode-pill')).not.toBeInTheDocument();

    // 菜单内容与会话内一致（含红线提示）
    await user.click(screen.getByTestId('composer-mode-menu'));
    expect(screen.getByText(SANDBOX_REDLINE_HINT)).toBeInTheDocument();
  });

  it('首页受控模式：选「自动审批」只回调 onChange，不触 PATCH；出现 sep + 带 × pill', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    requestLog.length = 0;
    const { rerender } = render(
      <ApprovalModeMenu sessionId={null} value="manual" onChange={onChange} variant="composer" />,
    );

    await user.click(screen.getByTestId('composer-mode-menu'));
    await user.click(screen.getByTestId('menu-item-auto'));
    expect(onChange).toHaveBeenCalledWith('auto');
    expect(requestLog.some((entry) => entry.method === 'PATCH')).toBe(false);

    // 受控值翻转后：tb-sep + .pill-close（关闭自动审批 + 自动徽标）
    rerender(<ApprovalModeMenu sessionId={null} value="auto" onChange={onChange} variant="composer" />);
    expect(screen.getByTestId('composer-mode-sep')).toBeInTheDocument();
    const pill = screen.getByTestId('composer-mode-pill');
    expect(pill).toHaveClass('pill-close');
    expect(pill).toHaveTextContent('关闭自主模式');
    expect(screen.getByTestId('composer-mode-menu')).toHaveTextContent('切换模式：自主模式');
  });

  it('pill × 点击回默认模式（R11 closePill 的 React 等价实现）', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ApprovalModeMenu sessionId={null} value="auto" onChange={onChange} variant="composer" />);

    await user.click(screen.getByTestId('composer-mode-pill'));
    expect(onChange).toHaveBeenCalledWith('manual');
  });

  it('会话内 composer 变体：AUTO 会话出现 pill，点 × PATCH 回 manual', async () => {
    const user = userEvent.setup();
    seedSession('auto');
    render(<ApprovalModeMenu sessionId="sess-mode" variant="composer" />);

    expect(screen.getByTestId('composer-mode-pill')).toBeInTheDocument();
    await user.click(screen.getByTestId('composer-mode-pill'));
    await waitFor(() => {
      expect(useSessionStore.getState().list[0]?.approval_mode).toBe('manual');
    });
    const patch = requestLog.at(-1);
    expect(patch).toMatchObject({ method: 'PATCH', body: { approval_mode: 'manual' } });
  });
});
