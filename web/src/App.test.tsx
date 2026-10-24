// @vitest-environment jsdom
/**
 * AppShell 与路由（A4 §9.1 / 任务 #2 交付判据）：
 * - `/` 空态首页：问候 + Composer（发送即建会话 → 导航到 /s/:id）；
 * - `/s/:sessionId` 深链：会话视图直开（种子 GET /sessions/{id} + 事件流连接；
 *   SSE 用 MSW 空流 stub，流式行为在 events/sse.test.ts 与 replay/* 覆盖）；
 * - M3 Phase 2：挂起审批 REST 恢复（AC-006-5）+ 同 id 幂等 + 审批模式菜单挂载；
 * - 右面板默认收起 + 展开 rail；侧栏折叠入口；断线 toast（connStore → Toast）。
 */
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';

import { ApprovalRequestEventSchema } from '@contracts/events';

import { renderAtRoute, server } from './tests/test-utils';
import { GREETINGS } from './App';
import { useApprovalStore } from './features/approval/store';
import { pendingApprovalA, pendingApprovalB } from './tests/msw-handlers';
import { HANDLERS } from './events/handlers';
import { useChatStore } from './features/chat/store';
import { useConnStore } from './stores/connStore';
import { useSessionStore } from './features/sessions/store';
import { useUiStore } from './stores/uiStore';
import { useDiffStore, useWorkbenchStore } from './features/workbench/store';

describe('AppShell · 路由与三栏（任务 #2）', () => {
  it('`/` 空态首页：时段问候 + Composer + 侧栏（列表加载）', async () => {
    renderAtRoute('/');
    // F29：问候按本地时段取五态之一（app-shell 口径）
    expect(GREETINGS).toContain(screen.getByTestId('home-greet').textContent);
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
      expect(GREETINGS).toContain(screen.getByTestId('home-greet').textContent);
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

  it('Ctrl+K 唤起 ⌘K 合并面板（F5/F25：打开的标签页 + 命令建议），Esc 关闭', async () => {
    const user = userEvent.setup();
    renderAtRoute('/');
    expect(screen.queryByTestId('command-palette')).not.toBeInTheDocument();

    await user.keyboard('{Control>}k{/Control}');
    expect(screen.getByTestId('command-palette')).toBeInTheDocument();
    expect(screen.getByTestId('command-palette-input')).toHaveAttribute(
      'placeholder',
      '搜索标签页或命令…',
    );
    // 首页无会话上下文：无任务条目，命令建议组在位
    expect(screen.queryByTestId('command-palette-opt-task:current')).not.toBeInTheDocument();
    expect(screen.getByTestId('command-palette-opt-new-task')).toBeInTheDocument();

    await user.type(screen.getByTestId('command-palette-input'), '{Escape}');
    expect(screen.queryByTestId('command-palette')).not.toBeInTheDocument();
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
      // currentId 由 SessionView 挂载 effect 写入（App.tsx setCurrent），晚于 header
      // 的 DOM 提交——断言必须在 waitFor 内等，否则会落在「header 已渲染、effect
      // 未跑」的调度窗口读到 null（CI flake 根因，慢机/节流可复现）
      expect(useSessionStore.getState().currentId).toBe('sess-new');
    });
    // 事件流连接随后建立（MSW 空流 stub）：连接态从 idle 出发
    expect(['idle', 'connecting', 'open', 'reconnecting']).toContain(useConnStore.getState().state);
  });

  it('右面板宽度持久化：store 写 localStorage（刷新恢复键 lcode.panel.width）', () => {
    renderAtRoute('/');
    useWorkbenchStore.getState().setWidth(420);
    expect(localStorage.getItem('lcode.panel.width')).toBe('420');
  });
});

describe('AppShell · 挂起审批恢复与审批模式（M3 Phase 2）', () => {
  /** 构造一条 approval_request SSE 事件（等价首连重放投递）。 */
  function replayApprovalRequest(
    approval: typeof pendingApprovalA,
    toolCallId: string,
    seq: number,
  ): void {
    const event = ApprovalRequestEventSchema.parse({
      seq,
      session_id: approval.session_id,
      run_id: 'run-replay',
      created_at: approval.created_at,
      type: 'approval_request',
      approval_id: approval.id,
      tool_call_id: toolCallId,
      tool: approval.tool,
      args: approval.args,
      risk_level: approval.risk_level,
      reason: approval.reason ?? null,
    });
    act(() => {
      HANDLERS.approval_request(event);
    });
  }

  it('刷新恢复：REST pending 合并入 store，SSE 重放后挂起卡带完整载荷与决策表单（AC-006-5）', async () => {
    renderAtRoute('/s/sess-a');
    await waitFor(() => {
      expect(useApprovalStore.getState().pendingIds).toEqual(['aprv-a1', 'aprv-a2']);
    });

    // SSE 首连重放：approval_request 重建流内卡片（生产语义：事件表重放）
    replayApprovalRequest(pendingApprovalA, 'call_replay_1', 901);
    replayApprovalRequest(pendingApprovalB, 'call_replay_2', 902);

    await waitFor(() => {
      expect(screen.getAllByTestId('approval-row')).toHaveLength(2);
    });
    const first = screen.getAllByTestId('approval-row')[0];
    expect(first).toHaveAttribute('data-status', 'pending');
    // Bash 变体命令原文完整可见（REST 载荷与 SSE 事件同 id 合并）+ 决策表单就绪
    expect(screen.getAllByTestId('approval-cmd')[0]).toHaveTextContent('rm -rf build/');
    expect(screen.getAllByTestId('decision-form')).toHaveLength(2);
    // 同 id 幂等：重放不重复入列
    expect(useApprovalStore.getState().pendingIds).toEqual(['aprv-a1', 'aprv-a2']);
    expect(useApprovalStore.getState().byId.size).toBe(2);
  });

  it('恢复幂等：REST 已恢复的挂起卡，SSE 重放同 id approval_request 不重复入列', async () => {
    renderAtRoute('/s/sess-a');
    await waitFor(() => {
      expect(useApprovalStore.getState().pendingIds).toEqual(['aprv-a1', 'aprv-a2']);
    });

    replayApprovalRequest(pendingApprovalA, 'call_replay', 910);

    expect(useApprovalStore.getState().pendingIds).toEqual(['aprv-a1', 'aprv-a2']);
    expect(useApprovalStore.getState().byId.size).toBe(2);
  });

  it('恢复失败降级：approvals 接口 5xx 不阻塞会话（会话视图照常可用）', async () => {
    server.use(
      http.get('*/api/v1/approvals', () =>
        HttpResponse.json({ code: 'INTERNAL_ERROR', message: 'boom' }, { status: 500 }),
      ),
    );
    renderAtRoute('/s/sess-a');
    await waitFor(() => expect(screen.getByTestId('session-header')).toBeInTheDocument());
    await waitFor(() => expect(useApprovalStore.getState().pendingIds).toEqual([]));
    // 会话本体仍可用：输入区 + 消息流在位
    expect(screen.getByTestId('composer')).toBeInTheDocument();
    expect(screen.getByTestId('message-scroll')).toBeInTheDocument();
  });

  it('审批模式菜单挂载在输入区工具条（AC-013-3/4；F12 原型口径：会话头部无模式钮，切换模式在 composer 工具条）', async () => {
    renderAtRoute('/s/sess-a');
    await waitFor(() => expect(screen.getByTestId('session-header')).toBeInTheDocument());
    expect(screen.getByTestId('composer-mode-menu')).toBeInTheDocument();
    await userEvent.setup().click(screen.getByTestId('composer-mode-menu'));
    expect(screen.getByText('沙箱内命令执行/文件写入始终需要人工审批，AUTO 不会放行它们')).toBeInTheDocument();
  });

  it('F2 态 A：头部「切换终端」钮与 Ctrl+J 切换主列底部 dock（bar ↔ hidden ↔ ~200px 展开区）', async () => {
    const user = userEvent.setup();
    useWorkbenchStore.getState().setTerminalDock('bar');
    renderAtRoute('/s/sess-a');
    await waitFor(() => expect(screen.getByTestId('session-header')).toBeInTheDocument());

    // 默认 dock 条可见（一行「终端」，宽度止于右栏边界——dock 在主列内渲染）
    expect(screen.getByTestId('terminal-dock')).toHaveTextContent('终端');
    // 头部钮 → 全收（hidden 不渲染）
    await user.click(screen.getByTestId('header-terminal'));
    expect(screen.queryByTestId('terminal-dock')).not.toBeInTheDocument();
    // Ctrl+J → 回 dock 条；点子标签展开 ~200px 终端区（TerminalView 只读回显）
    await user.keyboard('{Control>}j{/Control}');
    expect(screen.getByTestId('terminal-dock')).toBeInTheDocument();
    await user.click(screen.getByTestId('terminal-dock-expand'));
    expect(screen.getByTestId('terminal-dock-panel')).toHaveStyle({ height: '200px' });
    expect(screen.getByTestId('terminal-view')).toBeInTheDocument();
    // 复位：不给后续用例留 panel 态
    useWorkbenchStore.getState().setTerminalDock('bar');
  });

  it('F12 任务头部：标题 + workspace·main 徽标钮 + 右簇（分享/帮助/终端/面板）；运行中出更改 pill（有 diff_ready 用真实计数，点击开审查标签）', async () => {
    const user = userEvent.setup();
    renderAtRoute('/s/sess-a');
    await waitFor(() => expect(screen.getByTestId('session-header')).toBeInTheDocument());

    // 静态占位钮与徽标钮在位（F12 右簇）
    expect(screen.getByTestId('ws-badge')).toHaveTextContent('workspace');
    expect(screen.getByTestId('ws-badge')).toHaveTextContent('main');
    expect(screen.getByTestId('header-terminal')).toBeInTheDocument();
    expect(screen.getByTestId('header-panel')).toBeInTheDocument();

    // idle：无更改 pill
    expect(screen.queryByTestId('chg-pill')).not.toBeInTheDocument();

    // 运行中且无 diff_ready：pill 出现，回退原型演示计数 +2 -0
    useChatStore.getState().applyRunStatus({
      subject: 'run',
      phase: 'started',
      runId: 'run-chg',
      createdAt: '2026-10-18T00:00:00+00:00',
    });
    await waitFor(() => expect(screen.getByTestId('chg-pill')).toHaveTextContent('+2 -0'));

    // diff_ready 到达：pill 用真实计数；点击打开右栏审查标签（F15 诚实性）
    useDiffStore.getState().upsertFromEvent({
      reviewId: 'rev-chg-1',
      sessionId: 'sess-a',
      round: 1,
      baseline: { kind: 'head', ref: 'HEAD', commit: 'a'.repeat(40) },
      files: [
        { path: 'hello.py', old_path: null, change: 'added', additions: 17, deletions: 0, binary: false },
        { path: 'README.md', old_path: null, change: 'modified', additions: 0, deletions: 3, binary: false },
      ],
      additionsTotal: 17,
      deletionsTotal: 3,
      patchRef: '/x',
      createdAt: '2026-10-18T00:01:00+00:00',
    });
    await waitFor(() => expect(screen.getByTestId('chg-pill')).toHaveTextContent('+17 -3'));
    await user.click(screen.getByTestId('chg-pill'));
    expect(useWorkbenchStore.getState().activeTabId).toBe('review:rev-chg-1');
    expect(useWorkbenchStore.getState().collapsed).toBe(false);

    // run 收敛：pill 消失
    useChatStore.getState().applyRunStatus({
      subject: 'run',
      phase: 'finished',
      runId: 'run-chg',
      createdAt: '2026-10-18T00:02:00+00:00',
    });
    await waitFor(() => expect(screen.queryByTestId('chg-pill')).not.toBeInTheDocument());
  });
});
