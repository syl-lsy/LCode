import { useCallback, useEffect, useMemo, useState } from 'react';
import { Outlet, useNavigate, useParams } from 'react-router';

import type { MessageSendAck } from '@contracts/rest';

import { createSession, getSession, patchSession } from '@/api/sessions';
import { listApprovals } from '@/api/approvals';
import { sendMessage } from '@/api/messages';
import { ApiError } from '@/api/http';

import { deltaBuffers, terminalBuffer } from '@/events/buffers';
import { handleFrame, resetDispatcher } from '@/events/dispatcher';
import { streamManager } from '@/events/sse';

import { useApprovalStore } from '@/features/approval/store';
import { BranchSwitcher } from '@/features/chat/components/BranchSwitcher';
import { Composer } from '@/features/chat/Composer';
import { MessageList } from '@/features/chat/components/MessageList';
import { RunErrorBanner } from '@/features/chat/components/RunErrorBanner';
import { TaskMenus } from '@/features/chat/components/TaskMenus';
import { useChatStore } from '@/features/chat/store';
import { QuotaButton } from '@/features/costs/components/QuotaButton';
import { useCostStore } from '@/features/costs/store';
import { CommandPalette } from '@/features/workbench/components/CommandPalette';
import { EffortSelector } from '@/features/models/components/EffortSelector';
import { ModelSelector } from '@/features/models/components/ModelSelector';
import { useModelsStore } from '@/features/models/store';
import { Sidebar } from '@/features/sessions/components/Sidebar';
import { ApprovalModeMenu } from '@/features/sessions/components/ApprovalModeMenu';
import { formatRelativeTime } from '@/features/sessions/components/SessionItem';
import { useSessionStore } from '@/features/sessions/store';
import { useDiffStore, useTerminalStore, useWorkbenchStore } from '@/features/workbench/store';
import { WorkbenchPanel } from '@/features/workbench/components/WorkbenchPanel';
import { TerminalDock } from '@/features/workbench/terminal/TerminalDock';
import { useTodoStore } from '@/features/todo/store';

import { Toast } from '@/components/Toast';
import { HelpMenu } from '@/components/HelpMenu';
import { Icon, type IconName } from '@/components/Icon';

import { useConnStore } from '@/stores/connStore';
import { usePaletteStore } from '@/stores/paletteStore';
import { useUiStore } from '@/stores/uiStore';

// 壳层与首页空态页面级样式（来源 design/prototype/app-shell.html）
import './app-shell.css';
// 任务对话三态页面级样式（来源 design/prototype/task-conversation.html，B2）
import '@/features/chat/components/task-conversation.css';
// 改动文件卡 + 审查面板 + 文件预览页面级样式（来源 design/prototype/changed-files.html，B3；
// chat 与 workbench 两 feature 共同消费，故落 styles/ 全局引入）
import '@/styles/changed-files.css';
// 终端双容器页面级样式（来源 design/prototype/terminal-panel.html，B4）
import '@/features/workbench/terminal/terminal-panel.css';
// 侧边多用面板页面级样式（来源 design/prototype/side-panels.html，B4）
import '@/features/workbench/components/side-panels.css';

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
  const toggleTerminalDock = useWorkbenchStore((s) => s.toggleTerminalDock);
  const openReviewTab = useWorkbenchStore((s) => s.openReviewTab);
  const setPanelCollapsed = useWorkbenchStore((s) => s.setCollapsed);
  const reviews = useDiffStore((s) => s.reviews);
  const [seedError, setSeedError] = useState(false);

  // F12 更改 pill 的真实计数（F15 诚实性）：取本会话最近一次 diff_ready 的
  // additions/deletions；无真实数据时回退原型演示值（+2 -0），点击不开标签。
  const latestReview = useMemo(() => {
    let latest: { id: string; round: number; additionsTotal: number; deletionsTotal: number; createdAt: string } | null = null;
    for (const review of reviews.values()) {
      if (review.sessionId !== sessionId) continue;
      if (latest === null || review.createdAt > latest.createdAt) {
        latest = {
          id: review.id,
          round: review.round,
          additionsTotal: review.additionsTotal,
          deletionsTotal: review.deletionsTotal,
          createdAt: review.createdAt,
        };
      }
    }
    return latest;
  }, [reviews, sessionId]);
  const chgAdd = latestReview?.additionsTotal ?? 2;
  const chgDel = latestReview?.deletionsTotal ?? 0;

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

  // 原型 composer-bar 口径：切换模式钮在输入区工具条（ApprovalModeMenu composer 变体）
  const composerMode = useMemo(
    () => <ApprovalModeMenu sessionId={sessionId} variant="composer" />,
    [sessionId],
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* F12 任务头部（.task-head，来源 task-conversation.html）：标题 + workspace·main
          徽标钮 + 更多；右簇 = 分享/帮助/切换终端/展开侧边面板；运行中右上更改 pill */}
      <header data-testid="session-header" className="task-head">
        <h1>{sessionTitle.trim() === '' ? '会话' : sessionTitle}</h1>
        {/* F12 workspace·main 徽标钮（R3§D）：项目/分支数据 P1 前为静态占位 */}
        <button
          type="button"
          className="ws-badge"
          data-testid="ws-badge"
          aria-disabled="true"
          title="任务所在项目与分支：workspace · main（项目模型与分支切换器 P1 占位）"
        >
          <Icon name="folder" size={11} />
          workspace <span className="dot">·</span> main
        </button>
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
        {/* F26 任务「更多」菜单（12 项 + 3 分隔线）+ R4 分享勾选模式入口（B6）；
            重命名接 sessions PATCH 现有行为，回调在此接线（feature 隔离） */}
        <TaskMenus
          sessionId={sessionId}
          sessionTitle={sessionTitle.trim() === '' ? '会话' : sessionTitle}
          onRename={async (nextTitle) => {
            try {
              const session = await patchSession(sessionId, { title: nextTitle });
              useSessionStore.getState().upsert(session);
            } catch (error) {
              console.error('[sessions] 重命名会话失败：', error);
            }
          }}
        />
        {/* F24 帮助菜单（toolbar-menus 菜单 7）：头部帮助钮 */}
        <HelpMenu triggerTestId="header-help" />
        <button
          type="button"
          className="icon-btn"
          data-testid="header-terminal"
          title="切换终端 Ctrl+J"
          onClick={toggleTerminalDock}
        >
          <Icon name="terminal" size={14} />
        </button>
        <button
          type="button"
          className="icon-btn"
          data-testid="header-panel"
          title="展开侧边面板"
          aria-label="展开侧边面板"
          onClick={() => setPanelCollapsed(false)}
        >
          <Icon name="panel" size={14} />
        </button>
        {/* F12 更改 pill（R3§D）：badge-add 风；运行中即出现（首个审批前）；
            点击打开右栏审查标签（F15：计数以 diff_ready 事实为准） */}
        {runState === 'running' && (
          <button
            type="button"
            className="chg-pill"
            data-testid="chg-pill"
            title={
              latestReview !== null
                ? `更改 +${chgAdd} -${chgDel} · 点击打开审查标签`
                : '更改 +2 -0 · diff_ready 到达后显示真实计数并可打开审查标签'
            }
            onClick={() => {
              if (latestReview !== null) openReviewTab(latestReview.id, `审查 · 第 ${latestReview.round} 轮`);
            }}
          >
            更改 <span className="mono">+{chgAdd} -{chgDel}</span>
          </button>
        )}
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
      {/* .composer-zone（task-conversation.css，来源 task-conversation.html 输入区容器） */}
      <div className="composer-zone">
        <Composer
          sessionId={sessionId}
          onSent={onSent}
          knobs={knobs}
          mode={composerMode}
          trailing={<QuotaButton sessionId={sessionId} />}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 空态首页

/**
 * F29 时段化问候（R3§A，原型 app-shell 五态口径；深夜文案为 LCode 版）。
 * 导出供测试注入小时数断言边界。
 */
export const GREETINGS: readonly string[] = [
  '早上好，新的一天，有什么想让我帮忙的吗', // 清晨 05-08
  '上午好呀，有什么想让我帮忙的吗', // 上午 09-11
  '下午好，需要我接着处理什么吗', // 下午 12-17
  '傍晚好，今天想让我做点什么', // 傍晚 18-22
  '夜深啦，代码不急，先照顾好自己哦', // 深夜 23-04
];

export function greetingIndexForHour(hour: number): number {
  if (hour >= 5 && hour < 9) return 0;
  if (hour >= 9 && hour < 12) return 1;
  if (hour >= 12 && hour < 18) return 2;
  if (hour >= 18 && hour < 23) return 3;
  return 4;
}

/** 快捷入口 4 卡（app-shell .quick；icon 为 Icon.tsx 名称）。 */
const QUICK_CARDS: readonly { readonly label: string; readonly icon: IconName }[] = [
  { label: '周报总结', icon: 'file-text' },
  { label: '报错修复', icon: 'pulse' },
  { label: 'PPT 制作', icon: 'present' },
  { label: '闲时任务', icon: 'moon' },
];

export function HomeView() {
  const navigate = useNavigate();
  const [greetIndex, setGreetIndex] = useState(() => greetingIndexForHour(new Date().getHours()));
  /** 审批模式（首页受控值，建会话时随 SessionCreate.approval_mode 携带） */
  const [mode, setMode] = useState<'manual' | 'auto'>('manual');
  /** C-06 成本提醒胶囊（P2 计费前演示件）：本地可关 */
  const [quotaTipOpen, setQuotaTipOpen] = useState(true);
  /** 快捷卡注入 Composer 输入框 */
  const [prefill, setPrefill] = useState<{ text: string; id: number } | null>(null);

  const knobs = useMemo(
    () => (
      <>
        <ModelSelector />
        <EffortSelector />
      </>
    ),
    [],
  );

  const composerMode = useMemo(
    () => (
      <ApprovalModeMenu
        sessionId={null}
        value={mode}
        onChange={setMode}
        variant="composer"
      />
    ),
    [mode],
  );

  // 空态发送 = 先建会话再发消息（模型/档位/审批模式随 SessionCreate 携带），随后进入会话视图
  const onSendWithoutSession = useCallback(
    async (content: string): Promise<void> => {
      const selection = useModelsStore.getState().selected;
      const session = await createSession({
        model: selection?.model ?? null,
        reasoning_effort: selection?.effort ?? null,
        approval_mode: mode,
      });
      useSessionStore.getState().upsert(session);
      useModelsStore.getState().syncFromSession(session);
      await sendMessage(session.id, { content });
      navigate(`/s/${session.id}`);
    },
    [navigate, mode],
  );

  return (
    /* .main-center/.col（app-shell.css，来源 app-shell.html）：垂直居中列 */
    <div className="main-center scroll-thin">
      {/* F29：L 字母水印（对应官方 Z 水印） */}
      <div className="wm" aria-hidden="true">
        L
      </div>
      <div className="col">
        <button
          type="button"
          className="greet text-left"
          data-testid="home-greet"
          title="时段化问候（F29）：按本地时段切换文案；点击循环预览 5 个时段"
          onClick={() => setGreetIndex((i) => (i + 1) % GREETINGS.length)}
        >
          {GREETINGS[greetIndex]}
        </button>
        {/* 项目选择行（F23）：项目 chip + Git 分支钮并排；分支切换器 P1 出稿前为占位 */}
        <div className="proj-sel">
          <button
            type="button"
            className="icon-btn"
            aria-disabled="true"
            title="取消选择当前项目（占位）"
            style={{ width: 24, height: 24 }}
          >
            <Icon name="close" size={12} />
          </button>
          <button
            type="button"
            className="proj-chip"
            aria-disabled="true"
            title="项目选择（P1 占位）"
          >
            <Icon name="folder" size={12} className="text-foreground-subtlest" />
            选择项目：root
            <Icon name="chevron-down" size={11} className="chev" />
          </button>
          {/* F23 Git 分支切换器（toolbar-menus 菜单 6）：main 钮弹轻量 dialog（B5） */}
          <BranchSwitcher />
        </div>
        <div className="composer-wrap">
          {/* C-06 成本提醒胶囊（F22/R3§A）：悬浮工具条上方、× 可关；P2 计费前演示件 */}
          {quotaTipOpen && (
            <div className="quota-tip" data-testid="quota-tip">
              <Icon name="gift" size={12} />
              本月额度剩余 64% · 11-01 重置（P2 计费前演示件）
              <button
                type="button"
                className="qt-x"
                data-testid="quota-tip-close"
                title="关闭提醒"
                onClick={() => setQuotaTipOpen(false)}
              >
                <Icon name="close" size={9} />
              </button>
            </div>
          )}
          <Composer
            sessionId={null}
            onSendWithoutSession={onSendWithoutSession}
            knobs={knobs}
            mode={composerMode}
            trailing={<QuotaButton sessionId={null} />}
            prefill={prefill}
            placeholder="向 LCode 提问，使用 @ 添加上下文，使用 / 选择命令或能力"
          />
        </div>
        {/* 快捷入口 4 卡（app-shell .quick）：点击填入输入框 */}
        <div className="quick" data-testid="quick-cards">
          {QUICK_CARDS.map((card) => (
            <button
              key={card.label}
              type="button"
              data-testid="quick-card"
              data-card={card.label}
              onClick={() => setPrefill({ text: card.label, id: Date.now() })}
            >
              <Icon name={card.icon} size={14} className="qic" /> {card.label}
            </button>
          ))}
        </div>
        <div className="hint-line">
          LCode 快捷键：Ctrl+N 新任务 · Ctrl+K 标签页切换 · Ctrl+B 侧边栏 · Ctrl+J 终端（会话内） · Ctrl+O 工作区
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- AppShell

export default function App() {
  const navigate = useNavigate();
  const connState = useConnStore((s) => s.state);
  const sidebarCollapsed = useUiStore((s) => s.sidebarCollapsed);
  const toggleSidebar = useUiStore((s) => s.toggleSidebar);
  const panelCollapsed = useWorkbenchStore((s) => s.collapsed);
  const setPanelCollapsed = useWorkbenchStore((s) => s.setCollapsed);
  const toggleTerminalDock = useWorkbenchStore((s) => s.toggleTerminalDock);
  const currentSessionId = useSessionStore((s) => s.currentId);
  const currentSessionTitle = useSessionStore((s) =>
    s.currentId === null ? null : (s.list.find((item) => item.id === s.currentId)?.title ?? ''),
  );
  /** 任务 tab 右侧相对时间标注（⌘K「打开的标签页」条目，F5） */
  const currentSessionActiveAt = useSessionStore((s) =>
    s.currentId === null ? null : (s.list.find((item) => item.id === s.currentId)?.last_active_at ?? null),
  );
  const [offlineHidden, setOfflineHidden] = useState(false);

  /** 新建会话（侧栏钮 / ⌘K「新任务」/ Ctrl+N 共用同一流程；SessionsCreate {} + 导航） */
  const newTask = useCallback((): void => {
    void createSession({})
      .then((session) => {
        useSessionStore.getState().upsert(session);
        useModelsStore.getState().syncFromSession(session);
        navigate(`/s/${session.id}`);
      })
      .catch((error: unknown) => {
        console.error('[sessions] 新任务创建失败：', error);
      });
  }, [navigate]);

  // <1024px 默认收起右面板（§9.2 降级规则；仅初始判定，不持续跟随）
  useEffect(() => {
    if (window.innerWidth < 1024) setPanelCollapsed(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 全局键位（F5/F25 合并面板；原型快捷键约定）：Ctrl+B 侧栏 / Ctrl+K ⌘K 面板 /
  // Ctrl+N 新任务。Ctrl+J 终端不在此绑定——TerminalDock 挂载于会话视图且自带同一
  // 键位监听（含 dock hidden 态），此处再绑会同按键双触发（toggle 两次互相抵消）。
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent): void => {
      if ((event.ctrlKey || event.metaKey) && (event.key === 'b' || event.key === 'B')) {
        event.preventDefault();
        toggleSidebar();
      }
      if ((event.ctrlKey || event.metaKey) && (event.key === 'k' || event.key === 'K')) {
        event.preventDefault();
        usePaletteStore.getState().togglePalette();
      }
      if ((event.ctrlKey || event.metaKey) && (event.key === 'n' || event.key === 'N')) {
        event.preventDefault();
        newTask();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [toggleSidebar, newTask]);

  useEffect(() => {
    if (connState !== 'offline') setOfflineHidden(false);
  }, [connState]);

  // 用户手动关闭 offline toast：映射为 closed（Toast 内部对 closed 渲染 null）
  const toastState = offlineHidden && connState === 'offline' ? 'closed' : connState;

  return (
    /* .shell（components.css，来源 app-shell.html）：display:flex + 100vh 根容器 */
    <div className="shell overflow-hidden bg-background text-foreground">
      <Sidebar />
      {/* .main（app-shell.css，来源 app-shell.html）：flex:1 + 纵向 + relative */}
      <main className="main overflow-hidden">
        <Outlet />
        {/* .rail（app-shell.css）：帮助/终端/展开面板三钮；面板展开时隐藏（panel-open 口径
            = 条件渲染，行为等价 body.panel-open .rail { display:none }） */}
        {panelCollapsed && (
          <div className="rail" data-testid="panel-rail">
            {/* F24 帮助菜单（toolbar-menus 菜单 7）：rail 帮助钮 */}
            <HelpMenu triggerTestId="rail-help" />
            <button
              type="button"
              className="icon-btn"
              data-testid="rail-terminal"
              title="切换终端 Ctrl+J"
              onClick={toggleTerminalDock}
            >
              <Icon name="terminal" size={15} />
            </button>
            <button
              type="button"
              data-testid="panel-open-rail"
              title="展开侧边面板"
              aria-label="展开侧边面板"
              className="icon-btn"
              onClick={() => setPanelCollapsed(false)}
            >
              {/* 原型口径：app-shell.html rail「展开侧边面板」钮图标 */}
              <Icon name="panel" size={15} />
            </button>
          </div>
        )}
      </main>
      <WorkbenchPanel currentSessionId={currentSessionId} sessionTitle={currentSessionTitle} />
      <Toast state={toastState} onDismiss={() => setOfflineHidden(true)} />
      {/* ⌘K 面板（F5/F25 合并面板：打开的标签页 + 命令建议；Ctrl/Cmd+K 唤起） */}
      <CommandPalette
        sessionId={currentSessionId}
        sessionTitle={currentSessionTitle}
        taskTabTime={currentSessionActiveAt === null ? '' : formatRelativeTime(currentSessionActiveAt)}
        onNewTask={newTask}
      />
      {/* .side-switch（app-shell.css）：左下角切换侧边栏（Ctrl+B） */}
      <button
        type="button"
        data-testid={sidebarCollapsed ? 'sidebar-expand' : 'sidebar-collapse'}
        title={sidebarCollapsed ? '展开侧边栏 Ctrl+B' : '收起侧边栏 Ctrl+B'}
        aria-label={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'}
        className="side-switch"
        onClick={toggleSidebar}
      >
        <span className="mark" aria-hidden="true">
          L
        </span>
      </button>
    </div>
  );
}
