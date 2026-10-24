import { useState } from 'react';

import type { TodoItem } from '@contracts/common';

import { Icon } from '@/components/Icon';

import { useTodoStore } from '../store';

/**
 * 状态面板（F13 改型，REQ-005 / AC-005-3；来源 design/prototype/task-conversation.html
 * 状态卡 .status-card / 胶囊 .capsule）。
 *
 * - 位置由挂载方决定（MessageList：消息流顶部状态卡，原型同位）；
 * - 展开态 = 进程头（滑杆钮 + 「进程」 + todo 计数）+ Git 工具组行
 *   （「更改 +N -N ｜ main ▾ ｜ 提交或推送[disabled]」——「提交或推送」P0 置灰：
 *   沙箱内 git 提交策略未定）+ todo 列表；收起态 = 胶囊「展开状态 ｜ 更改 +N -N」；
 * - agent 未调用 write_todos 时只显示更改计数态（胶囊仅 chg-badge，无展开钮）；
 *   todo 与更改计数都为空时完全不渲染（AC-005-3 空态零占位）；
 * - 数据源：todoStore.snapshot（最近一次 write_todos 全量快照，行为语义不变）；
 *   更改计数由挂载方（chat 容器 MessageList）从 diffStore 汇总后经 props 传入
 *   （F15 诚实性：以 diff_ready 事实为准；diffStore 是封闭数据 store，todo 域不直读；
 *   changes === null 时不渲染任何更改徽标、以 title 披露——原型 +58 -0 是设计稿
 *   演示值，宁可少渲染也不伪造数据）。Git 分支/提交均为静态占位。
 */

/** 无 diff_ready 事实时的 title 披露（对齐 App.tsx 头部更改 pill 口径）。 */
const NO_DIFF_TITLE = '更改计数以 diff_ready 事件为准：本会话暂无 diff_ready 数据，到达后显示真实计数';

export interface TodoPanelProps {
  /** 更改计数（F13 快捷入口）：挂载方从本会话最近一次 diff_ready 汇总；null = 无真实数据 */
  readonly changes?: { readonly additions: number; readonly deletions: number } | null;
}

function TodoRow({ todo }: { todo: TodoItem }) {
  return (
    <li
      data-testid="todo-item"
      data-status={todo.status}
      className={todo.status === 'completed' ? 'done' : undefined}
    >
      <span className="mk" aria-hidden="true">
        {todo.status === 'completed' && <Icon name="check" size={9} />}
        {todo.status === 'in_progress' && (
          <span
            className="pulse-dot"
            style={{
              width: 5,
              height: 5,
              borderRadius: '50%',
              background: 'var(--color-brand)',
              boxShadow: 'var(--glow-running)',
            }}
          />
        )}
      </span>
      <span>{todo.content}</span>
    </li>
  );
}

export function TodoPanel({ changes = null }: TodoPanelProps) {
  const snapshot = useTodoStore((s) => s.snapshot);
  const [collapsed, setCollapsed] = useState(false);

  const hasTodos = snapshot.length > 0;
  // F15 诚实性：仅 diff_ready 事实存在时才渲染更改徽标（null = 不渲染，不伪造）
  const hasRealChanges = changes !== null;
  const completed = snapshot.filter((todo) => todo.status === 'completed').length;

  if (!hasTodos && !hasRealChanges) return null; // AC-005-3：空态零占位

  // agent 未调用 write_todos：只显示更改计数态（胶囊仅 chg-badge，无展开钮）
  const expanded = hasTodos && !collapsed;

  if (!expanded) {
    return (
      <div data-testid="todo-panel" data-collapsed="true" aria-label="任务状态">
        <div className="capsule" title={hasRealChanges ? undefined : NO_DIFF_TITLE}>
          {hasTodos && (
            <button
              type="button"
              data-testid="todo-panel-toggle"
              className="exp"
              aria-expanded="false"
              title="展开状态面板"
              onClick={() => setCollapsed(false)}
            >
              <Icon name="chevron-down" size={9} /> 展开状态
            </button>
          )}
          {hasRealChanges && (
            <span className="chg-badge" data-testid="todo-changes">
              {`更改 +${changes.additions} -${changes.deletions}`}
            </span>
          )}
        </div>
      </div>
    );
  }

  return (
    <div data-testid="todo-panel" data-collapsed="false" aria-label="任务状态" className="status-block">
      <div className="status-card">
        <div className="status-head">
          <button
            type="button"
            className="icon-btn"
            aria-disabled="true"
            title="状态面板展开策略（P1 占位）"
            style={{ width: 22, height: 22 }}
          >
            <Icon name="sliders" size={11} />
          </button>
          <span className="proc-tab">
            <Icon name="chevron-down" size={11} /> 进程
          </span>
          <span className="grow" aria-hidden="true" />
          <span data-testid="todo-progress" className="status-count">
            {completed}/{snapshot.length}
          </span>
          <button
            type="button"
            data-testid="todo-panel-toggle"
            className="icon-btn"
            aria-expanded="true"
            title="收起为胶囊"
            style={{ width: 22, height: 22 }}
            onClick={() => setCollapsed(true)}
          >
            <Icon name="chevron-up" size={11} />
          </button>
        </div>
        {/* F13 Git 工具组（R3§D/F）：更改 +N -N | main ▾ | 提交或推送[disabled]
            F15 诚实性：无 diff_ready 事实时不渲染更改徽标（title 披露待达） */}
        <div
          className="git-tools"
          data-testid="todo-git-tools"
          title={hasRealChanges ? undefined : NO_DIFF_TITLE}
        >
          {hasRealChanges && (
            <>
              <span className="gt-lb">更改</span>
              <span className="badge badge-add">+{changes.additions}</span>
              <span className="badge badge-del">-{changes.deletions}</span>
              <span className="tb-sep" aria-hidden="true" />
            </>
          )}
          <button
            type="button"
            className="gt-btn"
            aria-disabled="true"
            title="分支切换器（出稿：toolbar-menus 菜单 6；P1 占位）"
          >
            main <Icon name="chevron-down" size={9} />
          </button>
          <span className="grow" aria-hidden="true" />
          <button type="button" className="btn btn-sm" disabled title="P0 置灰：沙箱内 git 提交策略未定">
            提交或推送
          </button>
        </div>
        <ul className="status-list" data-testid="todo-list">
          {snapshot.map((todo, index) => (
            <TodoRow key={`${index}:${todo.content}`} todo={todo} />
          ))}
        </ul>
      </div>
    </div>
  );
}
