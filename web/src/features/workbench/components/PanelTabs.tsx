import { TASK_TAB_ID, useWorkbenchStore } from '../store';

import { Icon } from '@/components/Icon';

/**
 * 标签条（A4 §8.1 PanelTabs 行；R10/RD#45 通用容器。来源：
 * design/prototype/side-panels.html p-head，样式类 components/side-panels.css）。
 *
 * tablist 口径（F25）：搜索｜任务名（首标签，无 ×）｜功能/产物标签 ×｜＋ 新增｜收起。
 * - 任务名标签 = 会话首标签（RD#45）：展示层伪标签（不入 store.tabs，无关闭语义），
 *   点击激活 TASK_TAB_ID → WorkbenchPanel 分发 TaskSummaryPane；仅会话视图
 *   （sessionTitle 传入）渲染；
 * - 「搜索标签页」与「＋ 新增标签」都触发「打开标签页」选择器（原型同款）。选择器
 *   开合态由 WorkbenchPanel 持有（单实例语义：无标签空态本身就是选择器，此刻
 *   弹层不再重复渲染）；子标签 kind 特型：subagent → sa-tab（运行光点随 M4 启用）、
 *   浏览器占位 → p2-tab + P2 灰角标；
 * - 审查标签 M3 由 diff 流程经 openReviewTab 打开，不在选择器手开。
 */
export interface PanelTabsProps {
  /** 任务名标签文案（null = 无会话上下文，不渲染任务名标签） */
  sessionTitle?: string | null;
  /** 「打开标签页」选择器开合态（WorkbenchPanel 持有，保证单实例） */
  chooserOpen?: boolean;
  onToggleChooser?: () => void;
}

export function PanelTabs({ sessionTitle, chooserOpen = false, onToggleChooser }: PanelTabsProps) {
  const tabs = useWorkbenchStore((s) => s.tabs);
  const activeTabId = useWorkbenchStore((s) => s.activeTabId);
  const setActiveTab = useWorkbenchStore((s) => s.setActiveTab);
  const closeTab = useWorkbenchStore((s) => s.closeTab);
  const setCollapsed = useWorkbenchStore((s) => s.setCollapsed);

  /** 任务名标签（F25）：有会话标题才渲染（首页无会话上下文） */
  const taskTabTitle = sessionTitle != null && sessionTitle.trim() !== '' ? sessionTitle.trim() : sessionTitle != null ? '会话' : null;
  const toggleChooser = (): void => onToggleChooser?.();

  return (
    <div data-testid="panel-tabs" className="p-head">
      <button
        type="button"
        data-testid="panel-search-tab"
        aria-label="搜索标签页"
        aria-expanded={chooserOpen}
        title="搜索标签页"
        className="icon-btn h-[22px] w-[22px]"
        onClick={toggleChooser}
      >
        <Icon name="search" size={11} />
      </button>
      <div data-testid="panel-tab-strip" className="strip">
        {taskTabTitle !== null && (
          <button
            type="button"
            data-testid="panel-task-tab"
            className={`panel-tab task-tab${activeTabId === TASK_TAB_ID ? ' on' : ''}`}
            title={`任务名标签：${taskTabTitle}`}
            onClick={() => setActiveTab(TASK_TAB_ID)}
          >
            <span className="tt">{taskTabTitle}</span>
          </button>
        )}
        {tabs.map((tab) => (
          <div
            key={tab.id}
            data-testid="panel-tab"
            data-active={tab.id === activeTabId}
            className={`panel-tab${tab.id === activeTabId ? ' on' : ''}${
              tab.kind === 'subagent' ? ' sa-tab' : ''
            }${tab.id === 'placeholder:browser' ? ' p2-tab' : ''}`}
          >
            <button type="button" className="max-w-[120px] truncate" onClick={() => setActiveTab(tab.id)}>
              {tab.title}
            </button>
            {tab.id === 'placeholder:browser' && <span className="badge flex-none">P2</span>}
            <button
              type="button"
              aria-label={`关闭 ${tab.title}`}
              data-testid="panel-tab-close"
              className="x"
              onClick={() => closeTab(tab.id)}
            >
              <Icon name="close" size={9} />
            </button>
          </div>
        ))}
      </div>
      <button
        type="button"
        data-testid="panel-add-tab"
        aria-label="新增标签"
        aria-expanded={chooserOpen}
        title="新增标签"
        className="icon-btn h-[22px] w-[22px]"
        onClick={toggleChooser}
      >
        <Icon name="plus" size={12} />
      </button>
      <button
        type="button"
        data-testid="panel-collapse"
        aria-label="收起侧边面板"
        title="收起侧边面板"
        className="icon-btn h-[22px] w-[22px]"
        onClick={() => setCollapsed(true)}
      >
        <Icon name="panel" size={12} />
      </button>
    </div>
  );
}
