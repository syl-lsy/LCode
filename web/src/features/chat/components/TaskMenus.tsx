import { useEffect, useMemo, useState } from 'react';

import { useChatStore } from '../store';

import { Icon } from '@/components/Icon';
import { Menu, type MenuItem } from '@/components/Menu';
import { TASK_TAB_ID, useWorkbenchStore } from '@/features/workbench/store';
import { usePaletteStore } from '@/stores/paletteStore';

import './task-menus.css';

/**
 * 任务头部「更多」菜单 + 分享勾选模式（F26/R4；来源 design/prototype/task-menus.html，
 * 样式类 components/task-menus.css）。
 *
 * 「更多」= 12 项 + 3 分隔线（F26 校准：round3 笔记写「13 项」与其列表 12 项不符，
 * 按列表执行），条目逐字照原型；「在资源管理器中打开」在 macOS 本地化为
 * 「在 Finder 中打开」（RD#9：Web 形态仍置灰）。已有能力接真实行为：
 * 重命名（sessions PATCH，App 注入回调）、复制会话 ID（剪贴板）、查看调用轨迹
 * （打开任务名标签 = 会话轨迹查看器 P1 预留位）；其余占位（disabled + title 轻提示）。
 *
 * 分享 = 消息勾选模式（R4）：点头部「分享」进入，勾选若干用户消息后导出
 * Markdown（真实下载）；「分享」只读链接按钮 P2 多人部署前置灰；Esc 关闭面板后
 * 勾选态保留，残留「重新打开选择面板」按钮可重新进入；「取消」清空勾选并关闭。
 */

/** macOS 判定（「在资源管理器中打开」本地化；navigator.platform 通用可用）。 */
export function isMacPlatform(): boolean {
  if (typeof navigator === 'undefined') return false;
  return navigator.platform.startsWith('Mac');
}

export interface TaskMenusProps {
  readonly sessionId: string;
  /** 当前会话标题（重命名提示词的初值） */
  readonly sessionTitle: string;
  /**
   * 重命名回调（App 接 sessions PATCH + sessionStore.upsert；feature 隔离规则
   * 禁止 chat 直接 import sessions store）。
   */
  readonly onRename?: (nextTitle: string) => Promise<void> | void;
}

/** 选中消息 → 导出 Markdown（真实下载；导出目标 = Markdown 文件，R4 LCode 决策）。 */
export function buildShareMarkdown(chosen: readonly { readonly content: string }[]): string {
  const body = chosen.map((m) => `## 用户消息\n\n${m.content}`).join('\n\n---\n\n');
  return `# LCode 会话分享导出\n\n${body}\n`;
}

export function TaskMenus({ sessionId, sessionTitle, onRename }: TaskMenusProps) {
  const items = useChatStore((s) => s.items);
  const byId = useChatStore((s) => s.byId);
  const setActiveTab = useWorkbenchStore((s) => s.setActiveTab);
  const setPanelCollapsed = useWorkbenchStore((s) => s.setCollapsed);

  const [shareOpen, setShareOpen] = useState(false);
  /** 已勾选的用户消息 id（Esc 关闭后面板外保留，R4 残留态） */
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());

  // 消息流条目（顺序表）→ 用户消息 / 助手行两类；助手行不参与勾选（R4）
  const threadRows = useMemo(() => {
    const rows: { readonly kind: 'user' | 'assistant'; readonly id: string; readonly content: string }[] = [];
    for (const id of items) {
      const item = byId.get(id);
      if (item === undefined || item.kind !== 'message') continue;
      if (item.role === 'user') {
        rows.push({ kind: 'user', id: item.id, content: item.content ?? '' });
      } else {
        rows.push({ kind: 'assistant', id: item.id, content: '' });
      }
    }
    return rows;
  }, [items, byId]);

  // Esc 关闭分享面板（勾选态保留）；⌘K 面板在场时先让给 ⌘K
  useEffect(() => {
    if (!shareOpen) return;
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key !== 'Escape') return;
      if (usePaletteStore.getState().open) return;
      setShareOpen(false);
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [shareOpen]);

  const rename = (): void => {
    const next = window.prompt('重命名任务', sessionTitle);
    if (next === null) return; // 取消
    const title = next.trim();
    if (title === '' || title === sessionTitle) return;
    void onRename?.(title);
  };

  const copySessionId = (): void => {
    const clip = typeof navigator !== 'undefined' ? navigator.clipboard : undefined;
    if (clip === undefined) return;
    void clip.writeText(sessionId).catch(() => {
      // 剪贴板不可用（非安全上下文等）：静默失败，不伪造成功提示
    });
  };

  const openTrajectory = (): void => {
    // LCode 映射（RD#9）：查看调用轨迹 = 任务名标签（TaskSummaryPane 轨迹占位块）
    setActiveTab(TASK_TAB_ID);
    setPanelCollapsed(false);
  };

  const finderLabel = isMacPlatform() ? '在 Finder 中打开' : '在资源管理器中打开';
  const noLocalFsTitle = 'LCode Web 无本地文件系统语义，P2 桌面壳启用';

  const moreItems: MenuItem[] = [
    { key: 'pin', label: '置顶任务', leading: <Icon name="star" size={13} className="text-foreground-subtlest" />, disabled: true, title: '置顶任务（P1 占位）' },
    { key: 'rename', label: '重命名任务', leading: <Icon name="pencil" size={13} className="text-foreground-subtlest" />, onSelect: rename },
    { key: 'archive', label: '归档任务', leading: <Icon name="archive" size={13} className="text-foreground-subtlest" />, disabled: true, title: '归档任务（P1 占位）' },
    { key: 'mark-unread', label: '标记为未读', leading: <Icon name="bell" size={13} className="text-foreground-subtlest" />, disabled: true, title: '标记为未读（P1 占位）' },
    { key: 'sep-1', kind: 'separator' },
    { key: 'open-finder', label: finderLabel, leading: <Icon name="folder" size={13} className="text-foreground-subtlest" />, disabled: true, title: noLocalFsTitle },
    { key: 'copy-path', label: '复制路径', leading: <Icon name="link" size={13} className="text-foreground-subtlest" />, disabled: true, title: noLocalFsTitle },
    { key: 'copy-task-path', label: '复制任务路径', leading: <Icon name="link" size={13} className="text-foreground-subtlest" />, disabled: true, title: noLocalFsTitle },
    { key: 'copy-log-path', label: '复制日志路径', leading: <Icon name="link" size={13} className="text-foreground-subtlest" />, disabled: true, title: noLocalFsTitle },
    { key: 'copy-session-id', label: '复制会话 ID', leading: <Icon name="link" size={13} className="text-foreground-subtlest" />, onSelect: copySessionId },
    { key: 'goto-config', label: '前往配置', leading: <Icon name="settings" size={13} className="text-foreground-subtlest" />, disabled: true, title: '设置（P1 占位）' },
    { key: 'sep-2', kind: 'separator' },
    { key: 'trajectory', label: '查看调用轨迹', leading: <Icon name="chart" size={13} className="text-foreground-subtlest" />, onSelect: openTrajectory },
    { key: 'sep-3', kind: 'separator' },
    { key: 'feedback', label: '反馈问题', leading: <Icon name="triangle-alert" size={13} className="text-foreground-subtlest" />, disabled: true, title: '反馈问题（P1 占位）' },
  ];

  const userRows = threadRows.filter((row) => row.kind === 'user');
  const pickedCount = userRows.filter((row) => selected.has(row.id)).length;

  const toggleRow = (id: string): void => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const exportMarkdown = (): void => {
    const chosen = userRows.filter((row) => selected.has(row.id));
    if (chosen.length === 0) return;
    const blob = new Blob([buildShareMarkdown(chosen)], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'lcode-share.md';
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <>
      {/* F26 任务头部「更多」菜单：12 项 + 3 分隔线（逐字照原型） */}
      <Menu
        trigger={<Icon name="more" size={14} />}
        triggerClassName="icon-btn"
        triggerLabel="更多"
        triggerTestId="task-more"
        menuLabel="任务菜单"
        align="right"
        menuMaxWidth={260}
        items={moreItems}
      />
      <span className="head-sep" aria-hidden="true" />
      {/* R4 分享 = 消息勾选模式入口；打开时头部钮高亮（原型 .thead .icon-btn.hl） */}
      <button
        type="button"
        className={`icon-btn${shareOpen ? ' hl' : ''}`}
        data-testid="task-share"
        aria-label="分享"
        aria-pressed={shareOpen}
        title="分享（勾选消息后导出）"
        onClick={() => setShareOpen((v) => !v)}
      >
        <Icon name="share" size={14} />
      </button>

      {shareOpen && (
        <div className="share-sel-backdrop" data-testid="share-backdrop" onClick={() => setShareOpen(false)}>
          <div
            className="share-sel"
            role="dialog"
            aria-label="分享会话"
            data-testid="share-panel"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="thread scroll-thin">
              {threadRows.length === 0 && (
                <div className="a-msg">会话暂无可分享的消息。</div>
              )}
              {threadRows.map((row) =>
                row.kind === 'user' ? (
                  <label
                    key={row.id}
                    className={`u-msg${selected.has(row.id) ? ' picked' : ''}`}
                    data-testid="share-msg"
                    data-checked={selected.has(row.id)}
                  >
                    <input
                      type="checkbox"
                      checked={selected.has(row.id)}
                      onChange={() => toggleRow(row.id)}
                    />
                    <p>{row.content}</p>
                  </label>
                ) : (
                  <div key={row.id} className="a-msg">
                    助手回复（工具卡片与 markdown）— 助手消息不参与勾选。
                  </div>
                ),
              )}
            </div>
            <div className="sel-bar">
              <span className="cnt" data-testid="share-count">已选 {pickedCount} 条消息</span>
              <button
                type="button"
                className="btn btn-sm btn-ghost"
                data-testid="share-select-all"
                onClick={() => setSelected(new Set(userRows.map((row) => row.id)))}
              >
                全选
              </button>
              <button
                type="button"
                className="btn btn-sm btn-ghost"
                data-testid="share-clear"
                onClick={() => setSelected(new Set())}
              >
                清除
              </button>
              <span className="grow" />
              <span className="hint">Esc 关闭</span>
              <button
                type="button"
                className="btn btn-sm btn-primary"
                data-testid="share-link"
                disabled
                title="生成只读会话链接：P2 多人部署前按钮置灰（导出保留）"
              >
                分享
              </button>
              <button
                type="button"
                className="btn btn-sm"
                data-testid="share-export"
                disabled={pickedCount === 0}
                onClick={exportMarkdown}
              >
                导出 Markdown
              </button>
              <button
                type="button"
                className="btn btn-sm btn-ghost"
                data-testid="share-cancel"
                onClick={() => {
                  setSelected(new Set());
                  setShareOpen(false);
                }}
              >
                取消
              </button>
            </div>
          </div>
        </div>
      )}

      {/* R4 残留态：Esc 关闭面板后勾选保留，残留按钮可重新进入 */}
      {!shareOpen && pickedCount > 0 && (
        <div className="share-resid-wrap" data-testid="share-resid-wrap">
          <div className="resid">
            <button type="button" className="btn btn-sm" data-testid="share-reopen" onClick={() => setShareOpen(true)}>
              重新打开选择面板
            </button>
            <span className="hint">Esc 关闭面板后，勾选态保留，残留本按钮可重新进入。</span>
          </div>
        </div>
      )}
    </>
  );
}
