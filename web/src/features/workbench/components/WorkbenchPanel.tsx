import { useCallback, useRef, useState } from 'react';

import { useWorkbenchStore, TASK_TAB_ID, PANEL_WIDTH_MAX, PANEL_WIDTH_MIN } from '../store';
import type { WorkbenchTab } from '../store';

import { DiffReviewPanel } from './DiffReviewPanel';
import { FilePreviewPane } from './FilePreviewPane';
import { PanelChooser } from './PanelChooser';
import { PanelTabs } from './PanelTabs';
import { PlaceholderPane } from './PlaceholderPane';
import { TaskSummaryPane } from './TaskSummaryPane';
import { TerminalPane } from '../terminal/TerminalPane';

import { Icon, type IconName } from '@/components/Icon';

/**
 * 右栏多用面板壳（A4 §9.1 WorkbenchPanel 行 / §9.1；R10。来源：
 * design/prototype/side-panels.html，样式类 components/side-panels.css）。
 *
 * - 贴右全高；收起 = 宽度 0 + 滑出画布右缘（collapsed/width 由 workbenchStore
 *   持久化，A4 §9.1 localStorage `lcode.panel.*`）；
 * - 左缘拖拽分隔条：220–480px，拖拽实时写 workbenchStore.width（change 即持久化）；
 * - 标签内容按 kind 分发：terminal → TerminalPane（态 B bash 子标签行 + TerminalView
 *   只读回显）；review → DiffReviewPanel（M3，reviewId 从标签 id 解出）；
 *   subagent → M4 结构化占位；placeholder → FilePreviewPane（id 前缀
 *   `file-preview:` 的文件预览只读占位）或 PlaceholderPane（按标签 id 给类型化
 *   标题 + 说明的结构化占位态，RD#45）；
 * - 任务名标签（F25/RD#45）：tablist 首位的会话首标签（展示层伪标签），激活时
 *   分发 TaskSummaryPane（任务概要/调用轨迹占位块）；
 * - 无标签空态 = 「打开标签页」选择器（.chooser，面板内右上）：终端可开，其余为
 *   占位标签；审查由 diff_ready 流程自动打开（不可手开）。
 */

const DRAG_HANDLE_PX = 5;

/** review 标签 id 前缀（tabIdOf('review', id) 的产物形态，解析用）。 */
const REVIEW_TAB_PREFIX = 'review:';

/** 文件预览标签 id 前缀（DiffCard「选择打开方式」写入的 placeholder 标签）。 */
const FILE_PREVIEW_PREFIX = 'file-preview:';

/**
 * 结构化占位态元数据（RD#45：新增占位类型按类型给标题 + 说明，样式对齐原型；
 * 键 = store 落库标签 id = tabIdOf('placeholder', id)）。
 */
const PLACEHOLDER_META: Record<
  string,
  { icon: IconName; milestone: string; description: string }
> = {
  'placeholder:assist': {
    icon: 'message-square',
    milestone: 'P1',
    description:
      '辅助对话是与主任务同上下文的兄弟会话：回复流式走 message_delta，输入区 / 模型选择 / 上下文用量独立。P1 接入，当前为结构化占位。',
  },
  'placeholder:file-preview': {
    icon: 'file-text',
    milestone: 'P0',
    description:
      '文件预览标签由消息流改动文件卡「选择打开方式」触发（携带真实路径）后打开只读预览；直接从选择器打开时无具体文件，此处为说明占位。',
  },
  'placeholder:code-view': {
    icon: 'code',
    milestone: 'P1',
    description:
      '源代码查看 = 原始文本 + 行号 + 代码工具条（复制 / 换行开关 / 语言徽标），与文件预览（markdown 渲染态）为同一文件的两种打开方式，可并存为两个标签。P1 落位，当前为结构化占位。',
  },
  'placeholder:plan': {
    icon: 'review-check',
    milestone: 'P1',
    description:
      '完整计划是计划卡的面板化长驻态：plan_ready 生成 / plan_decision 记录决策，批准后作为标签长驻右栏（「打开对话」回链消息流）。P1 接入，当前为结构化占位。',
  },
  'placeholder:browser': {
    icon: 'globe',
    milestone: 'P2',
    description:
      '浏览器标签消费 browser_action 事件（P2 线框）：地址栏 + 视口区 + 操作按钮。当前为占位展示，不伪造功能。',
  },
};

interface TabContentProps {
  readonly tab: WorkbenchTab;
  readonly currentSessionId: string | null;
  readonly onCloseTab: (tabId: string) => void;
}

/** tabKind → 内容分发。 */
function TabContent({ tab, currentSessionId, onCloseTab }: TabContentProps) {
  if (tab.kind === 'terminal') {
    // 态 B：bash 子标签行 + TerminalView 只读回显（关闭钮 = 关闭当前终端标签）
    return <TerminalPane tabId={tab.id} onCloseTab={onCloseTab} />;
  }
  if (tab.kind === 'review') {
    // 标签 id = `review:<reviewId>`（openReviewTab/tabIdOf 写入），面板据此定位审查
    return <DiffReviewPanel reviewId={tab.id.slice(REVIEW_TAB_PREFIX.length)} currentSessionId={currentSessionId} />;
  }
  if (tab.kind === 'subagent') {
    return (
      <PlaceholderPane
        title="子智能体进度"
        milestone="M4"
        icon={<Icon name="user" size={18} />}
        description="task() 子智能体的完整流式进度（思考折叠块 / 工具卡两态 / markdown 流式，tab 名 = 任务名 · RD#12）将在 M4 落位。"
      />
    );
  }
  // 文件预览（changed-files.html 右栏预览区；P0 只读占位，路径从标签 id 解出）。
  // store 落库 id = tabIdOf(kind, id) = `placeholder:file-preview:<path>`。
  if (tab.id.startsWith(`placeholder:${FILE_PREVIEW_PREFIX}`)) {
    return (
      <FilePreviewPane path={tab.id.slice(`placeholder:${FILE_PREVIEW_PREFIX}`.length)} />
    );
  }
  // 其余 placeholder = 类型化结构占位（RD#45：按类型给标题 + 说明）
  const meta = PLACEHOLDER_META[tab.id];
  return (
    <PlaceholderPane
      title={tab.title}
      milestone={meta?.milestone ?? 'P1'}
      icon={meta !== undefined ? <Icon name={meta.icon} size={18} /> : undefined}
      description={meta?.description}
    />
  );
}

export interface WorkbenchPanelProps {
  /**
   * 当前会话 id（App 层自 sessionStore 下传；feature 隔离规则禁止 workbench 直接
   * import sessions store）。review 面板用它做会话防串台校验。缺省 null 时跳过。
   */
  readonly currentSessionId?: string | null;
  /**
   * 当前会话标题（App 层下传，feature 隔离规则同上）：任务名标签（F25）的标签
   * 文案。null/缺省 = 无会话上下文（首页），不渲染任务名标签。
   */
  readonly sessionTitle?: string | null;
}

export function WorkbenchPanel({ currentSessionId = null, sessionTitle = null }: WorkbenchPanelProps) {
  const width = useWorkbenchStore((s) => s.width);
  const collapsed = useWorkbenchStore((s) => s.collapsed);
  const tabs = useWorkbenchStore((s) => s.tabs);
  const activeTabId = useWorkbenchStore((s) => s.activeTabId);
  const setWidth = useWorkbenchStore((s) => s.setWidth);
  const closeTab = useWorkbenchStore((s) => s.closeTab);
  /** 「打开标签页」选择器开合态（单实例语义：无标签空态本身就是选择器，弹层不再重复渲染） */
  const [chooserOpen, setChooserOpen] = useState(false);

  const draggingRef = useRef(false);

  const onDragStart = useCallback(
    (event: React.MouseEvent): void => {
      event.preventDefault();
      draggingRef.current = true;
      const onMouseMove = (moveEvent: MouseEvent): void => {
        if (!draggingRef.current) return;
        // 贴右全高：宽度 = 视口宽 − 指针 x（§9.1），clamp 到 220–480
        setWidth(window.innerWidth - moveEvent.clientX);
      };
      const stop = (): void => {
        draggingRef.current = false;
        window.removeEventListener('mousemove', onMouseMove);
        window.removeEventListener('mouseup', stop);
      };
      window.addEventListener('mousemove', onMouseMove);
      window.addEventListener('mouseup', stop);
    },
    [setWidth],
  );

  const onCloseTab = useCallback((tabId: string) => closeTab(tabId), [closeTab]);

  if (collapsed) {
    return (
      <section
        data-testid="workbench-panel"
        data-collapsed="true"
        aria-hidden="true"
        className="w-0 overflow-hidden border-l-0"
      />
    );
  }

  const activeTab = tabs.find((tab) => tab.id === activeTabId) ?? tabs.at(-1) ?? null;
  const widthPx = Math.min(PANEL_WIDTH_MAX, Math.max(PANEL_WIDTH_MIN, width));
  /** 任务名标签（F25）：有会话上下文才存在；激活时 body 分发任务概要占位块 */
  const taskTabTitle =
    sessionTitle !== null && sessionTitle !== undefined && sessionTitle.trim() !== ''
      ? sessionTitle.trim()
      : sessionTitle !== null && sessionTitle !== undefined
        ? '会话'
        : null;
  const showTaskPane = taskTabTitle !== null && activeTabId === TASK_TAB_ID;
  /** 选择器是否已以「无标签空态」形态在场（此刻弹层不再重复渲染，单实例） */
  const emptyChooserVisible = !showTaskPane && activeTab === null;

  return (
    <section
      data-testid="workbench-panel"
      data-collapsed="false"
      className="relative flex h-full min-w-0 flex-none overflow-hidden border-l border-border bg-panel"
      style={{ width: widthPx }}
    >
      <div
        data-testid="panel-drag-handle"
        role="separator"
        aria-orientation="vertical"
        aria-label="拖拽调整面板宽度"
        title="拖拽调整面板宽度"
        className="absolute top-0 left-0 z-30 h-full cursor-col-resize hover:bg-accent-border"
        style={{ width: DRAG_HANDLE_PX, marginLeft: -DRAG_HANDLE_PX }}
        onMouseDown={onDragStart}
      />
      <div className="relative flex h-full min-w-0 flex-1 flex-col">
        <PanelTabs
          sessionTitle={taskTabTitle}
          chooserOpen={chooserOpen}
          onToggleChooser={() => setChooserOpen((v) => !v)}
        />
        <div data-testid="panel-body" className="p-body scroll-thin min-h-0">
          {showTaskPane ? (
            <TaskSummaryPane title={taskTabTitle} />
          ) : activeTab === null ? (
            /* 无标签空态 = 原型「打开标签页」选择器（.chooser，面板内右上） */
            <PanelChooser testId="panel-empty-chooser" onPick={() => setChooserOpen(false)} />
          ) : (
            <TabContent tab={activeTab} currentSessionId={currentSessionId} onCloseTab={onCloseTab} />
          )}
          {/* 「＋ 新增 / 搜索标签页」触发的选择器浮层（面板内右上，RD#15 不迁移）；
              空态选择器在场时不重复渲染（单实例） */}
          {chooserOpen && !emptyChooserVisible && (
            <PanelChooser onPick={() => setChooserOpen(false)} />
          )}
        </div>
      </div>
    </section>
  );
}
