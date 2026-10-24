import { Icon, type IconName } from '@/components/Icon';

import { useWorkbenchStore, type TabKind } from '../store';

/**
 * 「打开标签页」选择器（RD#45 开放集合；来源 design/prototype/side-panels.html，
 * 样式类 components/side-panels.css .chooser，面板内右上、RD#15 不迁移）。
 *
 * 两处消费：
 * - PanelTabs「搜索标签页 / ＋ 新增标签」触发的浮层；
 * - WorkbenchPanel 无标签空态（面板空态 = 多用面板口径，直接展示选择器）。
 *
 * 条目 = 全目录八类可开类型（任务名为会话首标签不在列，RD#45）：
 * 审查由 diff_ready 流程自动打开（不可手开）；终端为真实现；其余六类为结构化
 * 占位标签；尾部「…」= 更多类型以产物为准（开放集合，禁用占位）。
 */

interface ChooserEntry {
  readonly key: string;
  readonly label: string;
  readonly icon: IconName;
  /** 不可手开（aria-disabled 占位 + 轻提示） */
  readonly disabledTitle?: string;
  readonly open?: { kind: TabKind; id: string; title: string };
}

const ENTRIES: readonly ChooserEntry[] = [
  {
    key: 'assist',
    label: '辅助对话',
    icon: 'message-square',
    open: { kind: 'placeholder', id: 'assist', title: '辅助对话' },
  },
  {
    key: 'review',
    label: '审查',
    icon: 'review-check',
    disabledTitle: '审查标签由 diff_ready 流程自动打开',
  },
  {
    key: 'terminal',
    label: '终端',
    icon: 'terminal',
    open: { kind: 'terminal', id: 'main', title: '终端' },
  },
  {
    key: 'browser',
    label: '浏览器',
    icon: 'globe',
    open: { kind: 'placeholder', id: 'browser', title: '浏览器' },
  },
  {
    key: 'file-preview',
    label: '文件预览',
    icon: 'file-text',
    open: { kind: 'placeholder', id: 'file-preview', title: '文件预览' },
  },
  {
    key: 'code-view',
    label: '源代码查看',
    icon: 'code',
    open: { kind: 'placeholder', id: 'code-view', title: '源代码查看' },
  },
  {
    key: 'plan',
    label: '完整计划',
    icon: 'review-check',
    open: { kind: 'placeholder', id: 'plan', title: '完整计划' },
  },
  {
    key: 'subagent',
    label: '子智能体进度',
    icon: 'user',
    open: { kind: 'subagent', id: 'main', title: '子智能体进度' },
  },
];

export interface PanelChooserProps {
  /** 根节点 testid（浮层 = panel-chooser；空态 = panel-empty-chooser） */
  testId?: string;
  /** 选中条目后的回调（浮层场景用于收起浮层） */
  onPick?: () => void;
}

export function PanelChooser({ testId = 'panel-chooser', onPick }: PanelChooserProps) {
  const openTab = useWorkbenchStore((s) => s.openTab);

  return (
    <div className="chooser" data-testid={testId}>
      <h2>打开标签页</h2>
      <p>选择要在侧边面板中打开的标签。</p>
      {ENTRIES.map((entry) => {
        const open = entry.open;
        return (
          <button
            key={entry.key}
            type="button"
            data-testid={`panel-chooser-${entry.key}`}
            aria-disabled={entry.disabledTitle !== undefined ? 'true' : undefined}
            title={entry.disabledTitle}
            onClick={
              open === undefined
                ? undefined
                : () => {
                    openTab(open);
                    onPick?.();
                  }
            }
          >
            <Icon name={entry.icon} size={14} className="ic" /> {entry.label}
          </button>
        );
      })}
      {/* RD#45：「包括但不限于」= 开放集合，更多类型以产物为准 */}
      <button type="button" disabled data-testid="panel-chooser-more" title="更多类型以产物为准（RD#45 开放集合）">
        …
      </button>
      <div className="note mt-2">
        空态任务仅 审查/终端 等功能项；文件与产物类标签由改动文件卡或 agent 产物触发。
        <b>更多类型以产物为准</b>——「包括但不限于」= 开放集合（RD#45）。
      </div>
    </div>
  );
}
