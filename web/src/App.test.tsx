// @vitest-environment jsdom
/**
 * AppShell 与路由（A4 §9.1 / 任务 #2 交付判据）：
 * - `/` 空态首页：问候 + Composer（发送即建会话 → 导航到 /s/:id）；
 * - `/s/:sessionId` 深链：会话视图直开（种子 GET /sessions/{id} + 事件流连接；
 *   SSE 用 MSW 空流 stub，流式行为在 events/sse.test.ts 与 replay/* 覆盖）；
 * - 右面板默认收起 + 展开 rail；侧栏折叠入口；断线 toast（connStore → Toast）。
 */
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { renderAtRoute } from './tests/test-utils';
import { useChatStore } from './features/chat/store';
import { useConnStore } from './stores/connStore';
import { useSessionStore } from './features/sessions/store';
import { useUiStore } from './stores/uiStore';
import { useWorkbenchStore } from './features/workbench/store';

describe('AppShell · 路由与三栏（任务 #2）', () => {
  it('`/` 空态首页：问候 + Composer + 侧栏（列表加载）', async () => {
    renderAtRoute('/');
    expect(screen.getByText('有什么想让我帮忙的吗')).toBeInTheDocument();
    expect(screen.getByTestId('composer')).toBeInTheDocument();
    expect(screen.getByTestId('sidebar')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getAllByTestId('session-item')).toHaveLength(3);
    });
  });

  it('`/s/:sessionId` 深链：会话视图直开，种子会话 + 标题 + Composer', async () => {
    renderAtRoute('/s/sess-a');
    await waitFor(() => {
      expect(screen.getByTestId('session-header')).toHaveTextContent('修复登录超时');
    });
    expect(screen.getByTestId('composer')).toBeInTheDocument();
    expect(screen.getByTestId('message-scroll')).toBeInTheDocument();
    expect(useSessionStore.getState().currentId).toBe('sess-a');
  });

  it('会话不存在（SESSION_NOT_FOUND 终态）：回首页（不卡死在空视图）', async () => {
    renderAtRoute('/s/missing');
    await waitFor(() => {
      expect(screen.getByText('有什么想让我帮忙的吗')).toBeInTheDocument();
    });
  });

  it('右面板：默认收起（宽度 0）+ rail 展开入口 → 展开后出现标签条', async () => {
    const user = userEvent.setup();
    renderAtRoute('/');
    expect(screen.getByTestId('workbench-panel')).toHaveAttribute('data-collapsed', 'true');
    await user.click(screen.getByTestId('panel-open-rail'));
    expect(screen.getByTestId('workbench-panel')).toHaveAttribute('data-collapsed', 'false');
    expect(screen.getByTestId('panel-tabs')).toBeInTheDocument();
  });

  it('侧栏折叠入口：Ctrl+B 与按钮都走 uiStore.requestToggle（持久化键 lcode.sidebar）', async () => {
    const user = userEvent.setup();
    renderAtRoute('/');
    expect(screen.queryByTestId('sidebar-collapsed')).not.toBeInTheDocument();

    await user.click(screen.getByTestId('sidebar-collapse'));
    expect(useUiStore.getState().sidebarCollapsed).toBe(true);
    expect(screen.getByTestId('sidebar-collapsed')).toBeInTheDocument();
    expect(localStorage.getItem('lcode.sidebar')).toBe('1');

    // 键盘 Ctrl+B 切回
    await user.keyboard('{Control>}b{/Control}');
    expect(useUiStore.getState().sidebarCollapsed).toBe(false);
    expect(screen.getByTestId('sidebar')).toBeInTheDocument();
  });

  it('断线状态镜像 → Toast 显示（AC-003-3）：reconnecting 可见、offline 可关闭', async () => {
    const user = userEvent.setup();
    renderAtRoute('/');
    expect(screen.queryByTestId('toast-conn')).not.toBeInTheDocument();

    useConnStore.getState().setState('reconnecting');
    await waitFor(() => expect(screen.getByTestId('toast-conn')).toHaveTextContent('连接中断'));

    useConnStore.getState().setState('offline');
    await waitFor(() => expect(screen.getByTestId('toast-conn')).toHaveTextContent('连接已断开'));
    await user.click(screen.getByTestId('toast-dismiss'));
    expect(screen.queryByTestId('toast-conn')).not.toBeInTheDocument();
  });

  it('会话运行态在头部出徽标（run_status started → 运行中；finished → 消失）', async () => {
    renderAtRoute('/s/sess-a');
    await waitFor(() => expect(screen.getByTestId('session-header')).toBeInTheDocument());

    useChatStore.getState().applyRunStatus({
      subject: 'run',
      phase: 'started',
      runId: 'run-1',
      createdAt: '2026-10-18T00:00:00+00:00',
    });
    await waitFor(() => expect(screen.getByTestId('session-run-chip')).toHaveTextContent('运行中'));

    useChatStore.getState().applyRunStatus({
      subject: 'run',
      phase: 'finished',
      runId: 'run-1',
      createdAt: '2026-10-18T00:01:00+00:00',
    });
    await waitFor(() => expect(screen.queryByTestId('session-run-chip')).not.toBeInTheDocument());
  });

  it('切会话（深链间导航）清空上一会话的消息流（管线水位随会话复位）', async () => {
    const first = renderAtRoute('/s/sess-a');
    await waitFor(() => expect(screen.getByTestId('session-header')).toBeInTheDocument());
    act(() => {
      useChatStore.getState().appendUserMessage({
        id: 'u-old',
        content: '上一会话的消息',
        createdAt: 't',
        runId: null,
      });
    });
    expect(screen.getByTestId('message-user')).toHaveTextContent('上一会话的消息');

    await act(async () => {
      await first.router.navigate('/s/sess-b');
    });
    await waitFor(() => {
      expect(screen.queryByTestId('message-user')).not.toBeInTheDocument();
    });
    expect(screen.getByTestId('empty-state')).toHaveTextContent('还没有消息');
    // 侧栏当前项跟随路由
    expect(useSessionStore.getState().currentId).toBe('sess-b');
  });

  it('首页发送：建会话 → 发消息 → 导航到 /s/:id（空态发起链路）', async () => {
    const user = userEvent.setup();
    renderAtRoute('/');
    await waitFor(() => expect(screen.getByTestId('sidebar')).toBeInTheDocument());

    await user.type(screen.getByTestId('composer-input'), '第一次提问');
    await user.click(screen.getByTestId('composer-send'));

    await waitFor(() => {
      expect(screen.getByTestId('session-header')).toBeInTheDocument();
    });
    expect(useSessionStore.getState().currentId).toBe('sess-new');
    // 事件流连接随后建立（MSW 空流 stub）：连接态从 idle 出发
    expect(['idle', 'connecting', 'open', 'reconnecting']).toContain(useConnStore.getState().state);
  });

  it('右面板宽度持久化：store 写 localStorage（刷新恢复键 lcode.panel.width）', () => {
    renderAtRoute('/');
    useWorkbenchStore.getState().setWidth(420);
    expect(localStorage.getItem('lcode.panel.width')).toBe('420');
  });
});
