import { useCallback, useEffect, useMemo, useState } from 'react';
import { Outlet, useNavigate, useParams } from 'react-router';

import type { MessageSendAck } from '@contracts/rest';

import { createSession, getSession } from '@/api/sessions';
import { listApprovals } from '@/api/approvals';
import { sendMessage } from '@/api/messages';
import { ApiError } from '@/api/http';

import { deltaBuffers, terminalBuffer } from '@/events/buffers';
import { handleFrame, resetDispatcher } from '@/events/dispatcher';
import { streamManager } from '@/events/sse';

import { useApprovalStore } from '@/features/approval/store';
import { Composer } from '@/features/chat/Composer';
import { MessageList } from '@/features/chat/components/MessageList';
import { RunErrorBanner } from '@/features/chat/components/RunErrorBanner';
import { useChatStore } from '@/features/chat/store';
import { QuotaButton } from '@/features/costs/components/QuotaButton';
import { useCostStore } from '@/features/costs/store';
import { EffortSelector } from '@/features/models/components/EffortSelector';
import { ModelSelector } from '@/features/models/components/ModelSelector';
import { useModelsStore } from '@/features/models/store';
import { Sidebar } from '@/features/sessions/components/Sidebar';
import { ApprovalModeMenu } from '@/features/sessions/components/ApprovalModeMenu';
import { useSessionStore } from '@/features/sessions/store';
import { useDiffStore, useTerminalStore, useWorkbenchStore } from '@/features/workbench/store';
import { WorkbenchPanel } from '@/features/workbench/components/WorkbenchPanel';
import { TerminalDock } from '@/features/workbench/terminal/TerminalDock';
import { useTodoStore } from '@/features/todo/store';

import { Toast } from '@/components/Toast';

import { useConnStore } from '@/stores/connStore';
import { useUiStore } from '@/stores/uiStore';

/**
 * 三栏 AppShell（A4 §9.1，R3 app-shell）：
 *   [sidebar 264px，可折叠] [main flex-1：消息流 + TerminalDock + Composer] [WorkbenchPanel 0-480px]
 *
 * 路由（A4 §2.1 router.tsx）：/ = 空态首页（问候 + Composer，发送即建会话）；
 * /s/:sessionId = 会话视图（首连全量重放重建，AC-008-3）。
 * <1024px 时右面板默认收起（§9.2 一条降级规则，不做移动适配）。
 */

/** 会话级 stores 复位（进入/切换会话时；seq 以会话为单位，管线水位必须跟着换）。 */
function resetSessionScopedStores(): void {
  useChatStore.getState().reset();
  useTerminalStore.getState().reset();
  useDiffStore.getState().reset();
  useTodoStore.getState().reset();
  useApprovalStore.getState().reset();
  useCostStore.getState().reset();
  useConnStore.getState().reset();
  resetDispatcher(); // 去重水位是会话内单调 seq：切会话必须复位
  deltaBuffers.clear();
  terminalBuffer.clear();
}

/** streamManager 接线（帧 → dispatcher；状态/心跳 → connStore，§4.5）。 */
function wireStream(sessionId: string): void {
  streamManager.connect(sessionId, {
    onFrame: (frame) => {
      handleFrame(frame);
    },
    onState: (state) => {
      useConnStore.getState().setState(state);
    },
    onHeartbeat: () => {
      useConnStore.getState().noteHeartbeat();
    },
  });
}

// ---------------------------------------------------------------- 会话视图

export function SessionView() {
  const { sessionId = '' } = useParams();
  const navigate = useNavigate();
  const runState = useChatStore((s) => s.activeRun.state);
  const sessionTitle = useSessionStore(
    (s) => s.list.find((item) => item.id === sessionId)?.title ?? '',
  );
  const [seedError, setSeedError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    resetSessionScopedStores();
    useSessionStore.getState().setCurrent(sessionId);
    setSeedError(false);

    // 路径 A：打开/刷新会话——先种子（run_state/模型选择）再全量重放（§4.2）
    getSession(sessionId)
      .then((session) => {
        if (cancelled) return;
        useSessionStore.getState().upsert(session);
        useModelsStore.getState().syncFromSession(session);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        console.error('[session] 会话加载失败：', error);
        if (error instanceof ApiError && (error.code === 'SESSION_NOT_FOUND' || error.code === 'NOT_FOUND')) {
          navigate('/');
          return;
        }
        setSeedError(true); // 网络等瞬态错误：如实提示，不静默
      });

    // 挂起审批恢复（AC-006-5）：REST pending 列表合并进 approvalStore；与 SSE
    // 重放的 approval_request 同 id 幂等（upsert 同键）。失败降级不阻塞会话
    // （后端 approvals API 未就绪/瞬态错误时，SSE 重放仍可补齐挂起卡）。
    listApprovals({ status: 'pending', sessionId })
      .then((page) => {
        if (cancelled) return;
        if (page.items.length > 0) useApprovalStore.getState().upsertFromRest(page.items);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        console.warn('[approvals] 挂起审批恢复失败（不阻塞会话，等待 SSE 重放补齐）：', error);
      });

    wireStream(sessionId);
    return () => {
      cancelled = true;
      streamManager.close();
    };
  }, [sessionId, navigate]);

  const onSent = useCallback((ack: MessageSendAck): void => {
    streamManager.ensureCaughtUp(ack.event_seq); // 路径 B（§4.2）
  }, []);

  const knobs = useMemo(
    () => (
      <>
        <ModelSelector sessionId={sessionId} />
        <EffortSelector sessionId={sessionId} />
      </>
    ),
    [sessionId],
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header
        data-testid="session-header"
        className="flex h-10 flex-none items-center gap-2 border-b border-border px-4"
      >
        <h1 className="min-w-0 flex-1 truncate text-[length:var(--text-ui-base)] font-semibold text-foreground-strong">
          {sessionTitle.trim() === '' ? '会话' : sessionTitle}
        </h1>
        {runState === 'running' && (
          <span data-testid="session-run-chip" className="badge border-accent-border text-accent-foreground">
            <span className="spinner flex-none" aria-hidden="true" /> 运行中
          </span>
        )}
        {runState === 'waiting_approval' && (
          <span data-testid="session-run-chip" className="badge border-accent-border text-accent-foreground">
            等待确认
          </span>
        )}
        <ApprovalModeMenu sessionId={sessionId} />
      </header>
      {seedError && (
        <p role="alert" className="px-4 pt-2 text-[length:var(--text-ui-xs)] text-destructive">
          会话加载失败（网络异常），事件流仍在重试；可刷新重试。
        </p>
      )}
      {/* B2：run 终态 error 横幅（store 驱动，新 run 开始自动消失），置于消息流上方 */}
      <RunErrorBanner />
      <MessageList sessionId={sessionId} />
      <TerminalDock />
      <Composer
        sessionId={sessionId}
        onSent={onSent}
        knobs={knobs}
        trailing={<QuotaButton sessionId={sessionId} />}
      />
    </div>
  );
}

// ---------------------------------------------------------------- 空态首页

export function HomeView() {
  const navigate = useNavigate();

  const knobs = useMemo(
    () => (
      <>
        <ModelSelector />
        <EffortSelector />
      </>
    ),
    [],
  );

  // 空态发送 = 先建会话再发消息（模型/档位随 SessionCreate 携带），随后进入会话视图
  const onSendWithoutSession = useCallback(
    async (content: string): Promise<void> => {
      const selection = useModelsStore.getState().selected;
      const session = await createSession({
        model: selection?.model ?? null,
        reasoning_effort: selection?.effort ?? null,
      });
      useSessionStore.getState().upsert(session);
      useModelsStore.getState().syncFromSession(session);
      await sendMessage(session.id, { content });
      navigate(`/s/${session.id}`);
    },
    [navigate],
  );

  return (
    <div className="flex h-full min-h-0 flex-col items-center justify-center overflow-y-auto">
      <div className="w-full max-w-[720px] px-6">
        <p className="mb-3.5 text-[length:var(--text-ui-xl)] font-semibold tracking-tight text-foreground-strong">
          有什么想让我帮忙的吗
        </p>
        <Composer sessionId={null} onSendWithoutSession={onSendWithoutSession} knobs={knobs} />
        <div className="mx-auto mt-1 max-w-[760px] text-center text-[length:var(--text-ui-2xs)] text-foreground-subtlest">
          发送后将自动创建会话；Ctrl+N 新会话 · Ctrl+B 侧边栏
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- AppShell

export default function App() {
  const connState = useConnStore((s) => s.state);
  const currentSessionId = useSessionStore((s) => s.currentId);
  const sidebarCollapsed = useUiStore((s) => s.sidebarCollapsed);
  const toggleSidebar = useUiStore((s) => s.toggleSidebar);
  const panelCollapsed = useWorkbenchStore((s) => s.collapsed);
  const setPanelCollapsed = useWorkbenchStore((s) => s.setCollapsed);
  const [offlineHidden, setOfflineHidden] = useState(false);

  // <1024px 默认收起右面板（§9.2 降级规则；仅初始判定，不持续跟随）
  useEffect(() => {
    if (window.innerWidth < 1024) setPanelCollapsed(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Ctrl+B 切换侧栏（原型快捷键约定）
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent): void => {
      if ((event.ctrlKey || event.metaKey) && (event.key === 'b' || event.key === 'B')) {
        event.preventDefault();
        toggleSidebar();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [toggleSidebar]);

  useEffect(() => {
    if (connState !== 'offline') setOfflineHidden(false);
  }, [connState]);

  // 用户手动关闭 offline toast：映射为 closed（Toast 内部对 closed 渲染 null）
  const toastState = offlineHidden && connState === 'offline' ? 'closed' : connState;

  return (
    <div className="flex h-screen overflow-hidden bg-background text-foreground">
      <Sidebar />
      <main className="relative flex min-w-0 flex-1 flex-col overflow-hidden">
        <Outlet />
        {panelCollapsed && (
          <button
            type="button"
            data-testid="panel-open-rail"
            title="展开侧边面板"
            aria-label="展开侧边面板"
            className="absolute top-2 right-2.5 z-20 flex h-8 w-8 items-center justify-center rounded-md border border-border bg-card text-foreground-subtle hover:bg-surface-hover hover:text-foreground-strong"
            onClick={() => setPanelCollapsed(false)}
          >
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
              <rect x="3" y="4" width="18" height="16" rx="2" />
              <path d="M15 4v16" />
            </svg>
          </button>
        )}
      </main>
      <WorkbenchPanel currentSessionId={currentSessionId} />
      <Toast state={toastState} onDismiss={() => setOfflineHidden(true)} />
      <button
        type="button"
        data-testid={sidebarCollapsed ? 'sidebar-expand' : 'sidebar-collapse'}
        title={sidebarCollapsed ? '展开侧边栏 Ctrl+B' : '收起侧边栏 Ctrl+B'}
        aria-label={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'}
        className="fixed bottom-3 left-3 z-40 flex h-8 items-center rounded-md border border-border bg-card/85 px-2.5 backdrop-blur hover:border-border-hover"
        onClick={toggleSidebar}
      >
        <span
          aria-hidden="true"
          className="flex h-[18px] w-[18px] items-center justify-center rounded-[5px] bg-primary font-mono text-[10px] font-bold text-primary-foreground"
        >
          L
        </span>
        <span aria-hidden="true" className="ml-1.5 text-foreground-subtlest">
          {sidebarCollapsed ? '⟩' : '⟨'}
        </span>
      </button>
    </div>
  );
}
