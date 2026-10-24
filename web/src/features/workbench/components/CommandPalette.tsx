import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router';

import { useUiStore } from '@/stores/uiStore';
import { usePaletteStore } from '@/stores/paletteStore';

import { EmptyState } from '@/components/EmptyState';
import { Icon, type IconName } from '@/components/Icon';

import { TASK_TAB_ID, useWorkbenchStore } from '../store';

import './command-palette.css';

/**
 * ⌘K 面板（F5/F25 合并面板；来源 design/prototype/command-palette.html，样式类
 * components/command-palette.css，收在 .cmdk 命名空间下）。
 *
 * 定性（F5）：官方 ⌘K = 「打开的标签页」切换器（任务 tab + 面板 tab + 输入过滤，
 * 无范围标签）；LCode 合并为一个 ⌘K 面板 =「打开的标签页」组 + 命令建议组
 * （RD#5 增补），范围标签维持「全部 / 操作 / 任务」（RD#5；「文件」范围在代码
 * 索引二期就绪前空置，不渲染）。侧边栏「搜索」钮同指向本面板（F25）。
 *
 * 真实切换语义（B6）：
 * - 面板 tab 选中 → workbenchStore.setActiveTab + 展开面板（收起时一并展开）；
 * - 任务 tab 选中 → 会话路由导航（仅一个任务 tab：回当前会话视图；标签 × =
 *   关闭任务标签 → 回首页）；
 * - 命令项已有能力接真实行为（新任务/切换侧边栏/切换终端/主题/添加终端与
 *   浏览器标签），其余置灰占位（title 轻提示）。
 *
 * 会话数据经 props 注入（feature 隔离规则：workbench 不得直接 import sessions）；
 * 「新任务」同理由 App 注入回调（涉及 sessions REST 与路由导航）。
 */

interface TabEntry {
  readonly kind: 'tab';
  readonly key: string;
  readonly icon: IconName;
  readonly title: string;
  /** 右侧 mono 标注（任务 tab = 相对时间；面板 tab = 「面板」） */
  readonly tm: string;
  readonly active: boolean;
  /** 任务 tab 恒可关（= 离开会话视图回首页）；面板 tab 走 closeTab */
  readonly onClose: () => void;
  readonly onActivate: () => void;
}

interface CmdEntry {
  readonly kind: 'cmd';
  readonly key: string;
  readonly icon: IconName;
  readonly label: string;
  readonly kbd?: string;
  readonly disabled?: boolean;
  readonly title?: string;
  readonly onActivate?: () => void;
}

type Entry = TabEntry | CmdEntry;

interface Group {
  readonly title: string;
  readonly entries: readonly Entry[];
}

type Scope = 'all' | 'actions' | 'tasks';

const SCOPES: readonly { readonly id: Scope; readonly label: string }[] = [
  { id: 'all', label: '全部' },
  { id: 'actions', label: '操作' },
  { id: 'tasks', label: '任务' },
];

/** 面板标签 kind → 图标（terminal/review 真实类；占位类给通用文件图标）。 */
function tabIcon(kind: string): IconName {
  switch (kind) {
    case 'terminal':
      return 'terminal';
    case 'review':
      return 'chart';
    case 'subagent':
      return 'user';
    default:
      return 'file-text';
  }
}

export interface CommandPaletteProps {
  /** 当前会话 id（null = 无会话上下文/首页：不渲染任务 tab 条目） */
  readonly sessionId: string | null;
  /** 当前会话标题（任务 tab 条目文案） */
  readonly sessionTitle: string | null;
  /** 任务 tab 右侧相对时间标注（App 以 formatRelativeTime 算好传入） */
  readonly taskTabTime?: string;
  /** 「新任务」命令回调（App 接 createSession + 导航；缺省置灰占位） */
  readonly onNewTask?: () => void;
}

export function CommandPalette({ sessionId, sessionTitle, taskTabTime, onNewTask }: CommandPaletteProps) {
  const open = usePaletteStore((s) => s.open);
  const closePalette = usePaletteStore((s) => s.closePalette);
  const navigate = useNavigate();

  const tabs = useWorkbenchStore((s) => s.tabs);
  const activeTabId = useWorkbenchStore((s) => s.activeTabId);
  const setActiveTab = useWorkbenchStore((s) => s.setActiveTab);
  const closeTab = useWorkbenchStore((s) => s.closeTab);
  const setCollapsed = useWorkbenchStore((s) => s.setCollapsed);
  const toggleTerminalDock = useWorkbenchStore((s) => s.toggleTerminalDock);
  const openTab = useWorkbenchStore((s) => s.openTab);

  const theme = useUiStore((s) => s.theme);
  const toggleTheme = useUiStore((s) => s.toggleTheme);
  const toggleSidebar = useUiStore((s) => s.toggleSidebar);

  const [scope, setScope] = useState<Scope>('all');
  const [query, setQuery] = useState('');
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const prevOpenRef = useRef(false);

  const taskTabTitle =
    sessionTitle !== null && sessionTitle.trim() !== '' ? sessionTitle.trim() : '会话';

  const groups = useMemo<Group[]>(() => {
    const tabEntries: TabEntry[] = [];
    if (sessionId !== null && sessionId !== '') {
      tabEntries.push({
        kind: 'tab',
        key: TASK_TAB_ID,
        icon: 'terminal',
        title: taskTabTitle,
        tm: taskTabTime ?? '',
        active: activeTabId === TASK_TAB_ID,
        onClose: () => navigate('/'),
        onActivate: () => navigate(`/s/${sessionId}`),
      });
    }
    for (const tab of tabs) {
      tabEntries.push({
        kind: 'tab',
        key: tab.id,
        icon: tabIcon(tab.kind),
        title: tab.title,
        tm: '面板',
        active: tab.id === activeTabId,
        onClose: () => closeTab(tab.id),
        onActivate: () => {
          setCollapsed(false);
          setActiveTab(tab.id);
        },
      });
    }

    const suggest: CmdEntry[] = [
      {
        kind: 'cmd',
        key: 'new-task',
        icon: 'plus',
        label: '新任务',
        kbd: 'Ctrl N',
        disabled: onNewTask === undefined,
        title: onNewTask === undefined ? '新任务（需会话上下文）' : undefined,
        onActivate: onNewTask,
      },
      {
        kind: 'cmd',
        key: 'open-workspace',
        icon: 'folder',
        label: '打开工作区',
        kbd: 'Ctrl O',
        disabled: true,
        title: '打开工作区（本地目录语义，P2 桌面壳启用）',
      },
      { kind: 'cmd', key: 'settings-1', icon: 'settings', label: '设置', disabled: true, title: '设置（P1 占位）' },
    ];
    const panels: CmdEntry[] = [
      { kind: 'cmd', key: 'toggle-sidebar', icon: 'panel-left', label: '切换侧边栏', kbd: 'Ctrl B', onActivate: toggleSidebar },
      { kind: 'cmd', key: 'toggle-terminal', icon: 'terminal', label: '切换终端', kbd: 'Ctrl J', onActivate: toggleTerminalDock },
      { kind: 'cmd', key: 'toggle-preview', icon: 'panel', label: '切换预览', disabled: true, title: '预览标签 P2 占位' },
      { kind: 'cmd', key: 'add-terminal', icon: 'plus', label: '添加终端标签', onActivate: () => openTab({ kind: 'terminal', id: 'main', title: '终端' }) },
      { kind: 'cmd', key: 'add-browser', icon: 'plus', label: '添加浏览器标签', onActivate: () => openTab({ kind: 'placeholder', id: 'browser', title: '浏览器' }) },
      {
        kind: 'cmd',
        key: 'add-review',
        icon: 'plus',
        label: '添加审查标签',
        disabled: true,
        title: '审查标签由 diff_ready 流程自动打开',
      },
    ];
    const config: CmdEntry[] = [
      { kind: 'cmd', key: 'settings-2', icon: 'settings', label: '设置', disabled: true, title: '设置（P1 占位）' },
      {
        kind: 'cmd',
        key: 'toggle-theme',
        icon: 'moon',
        label: theme === 'dark' ? '切换主题到浅色' : '切换主题到深色',
        onActivate: toggleTheme,
      },
      { kind: 'cmd', key: 'skills', icon: 'sparkle', label: '技能', disabled: true, title: '技能（P1 占位）' },
      { kind: 'cmd', key: 'mcp', icon: 'settings', label: 'MCP 服务器', disabled: true, title: 'MCP 服务器（P1 占位）' },
    ];
    const app: CmdEntry[] = [
      { kind: 'cmd', key: 'report-issue', icon: 'triangle-alert', label: '问题上报', disabled: true, title: '问题上报（P1 占位）' },
      { kind: 'cmd', key: 'docs', icon: 'file-text', label: '产品文档', disabled: true, title: '产品文档（P1 占位）' },
      { kind: 'cmd', key: 'connect', icon: 'globe', label: '连接', disabled: true, title: '连接（P2 占位）' },
    ];
    return [
      { title: '打开的标签页', entries: tabEntries },
      { title: '建议', entries: suggest },
      { title: '面板', entries: panels },
      { title: '配置', entries: config },
      { title: '应用', entries: app },
    ];
  }, [
    sessionId, taskTabTitle, taskTabTime, activeTabId, tabs, closeTab, setCollapsed, setActiveTab,
    onNewTask, toggleSidebar, toggleTerminalDock, openTab, theme, toggleTheme, navigate,
  ]);

  const visibleGroups = useMemo<Group[]>(() => {
    const keyword = query.trim().toLowerCase();
    return groups
      .filter((group) => {
        if (scope === 'tasks') return group.title === '打开的标签页';
        if (scope === 'actions') return group.title !== '打开的标签页';
        return true;
      })
      .map((group) => ({
        title: group.title,
        entries: group.entries.filter((entry) => {
          if (keyword === '') return true;
          const text = entry.kind === 'tab' ? entry.title : entry.label;
          return text.toLowerCase().includes(keyword);
        }),
      }))
      .filter((group) => group.entries.length > 0);
  }, [groups, scope, query]);

  const flat = useMemo(() => visibleGroups.flatMap((group) => group.entries), [visibleGroups]);
  const cursorIndex = Math.min(cursor, Math.max(0, flat.length - 1));

  // 每次打开：清过滤，光标初始落在当前激活 tab（原型「当前态高亮」口径）；
  // groups 随 store 变化重建，但仅在 open 跳变沿重置（不抢用户导航中的光标）
  useEffect(() => {
    if (open && !prevOpenRef.current) {
      setQuery('');
      setScope('all');
      const flatAll = groups.flatMap((group) => group.entries);
      const activeIdx = flatAll.findIndex((entry) => entry.kind === 'tab' && entry.active);
      setCursor(activeIdx >= 0 ? activeIdx : 0);
    }
    prevOpenRef.current = open;
  }, [open, groups]);

  // 过滤结果变化后光标越界时归位（保持键盘导航可用）
  useEffect(() => {
    setCursor((c) => Math.min(c, Math.max(0, flat.length - 1)));
  }, [flat.length]);

  if (!open) return null;

  const activate = (entry: Entry | undefined): void => {
    if (entry === undefined) return;
    if (entry.kind === 'tab') {
      entry.onActivate();
      closePalette();
      return;
    }
    if (entry.disabled === true || entry.onActivate === undefined) return;
    entry.onActivate();
    closePalette();
  };

  const closeEntry = (entry: Entry, event: React.MouseEvent): void => {
    event.stopPropagation();
    if (entry.kind !== 'tab') return;
    entry.onClose();
    setCursor(0);
  };

  const onInputKeyDown = (event: React.KeyboardEvent): void => {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setCursor(Math.min(cursorIndex + 1, Math.max(0, flat.length - 1)));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setCursor(Math.max(cursorIndex - 1, 0));
    } else if (event.key === 'Enter') {
      event.preventDefault();
      activate(flat[cursorIndex]);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      closePalette();
    } else if (event.key === 'Tab') {
      // 范围切换（empty-states #5 口径：「换个关键词，或用 Tab 切换范围」）
      event.preventDefault();
      setScope((s) => {
        const idx = SCOPES.findIndex((item) => item.id === s);
        return SCOPES[(idx + 1) % SCOPES.length]?.id ?? 'all';
      });
      setCursor(0);
    }
  };

  let optionIndex = -1;

  return (
    <div className="cmdk-backdrop" data-testid="command-palette-backdrop" onClick={closePalette}>
      <div className="cmdk-stage">
        <div className="cmdk" onClick={(event) => event.stopPropagation()}>
          <div
            className="dlg"
            role="dialog"
            aria-label="命令面板"
            data-testid="command-palette"
          >
            <div className="search">
              <Icon name="search" size={14} className="ic" />
              <input
                ref={inputRef}
                autoFocus
                value={query}
                placeholder="搜索标签页或命令…"
                aria-label="搜索标签页或命令"
                data-testid="command-palette-input"
                onChange={(event) => {
                  setQuery(event.target.value);
                  setCursor(0);
                }}
                onKeyDown={onInputKeyDown}
              />
            </div>
            <div className="scopes" role="tablist" aria-label="命令中心范围">
              {SCOPES.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  role="tab"
                  aria-selected={scope === item.id}
                  data-testid={`command-palette-scope-${item.id}`}
                  onClick={() => {
                    setScope(item.id);
                    setCursor(0);
                  }}
                >
                  {item.label}
                </button>
              ))}
            </div>
            <div className="list scroll-thin" role="listbox" aria-label="Suggestions">
              {flat.length === 0 ? (
                <EmptyState
                  testId="command-palette-empty"
                  icon={<Icon name="search-minus" size={16} />}
                  title="没有匹配的命令"
                  description="换个关键词，或用 Tab 切换范围。LCode 裁剪：「文件」范围在代码索引（二期）就绪前空置。"
                />
              ) : (
                visibleGroups.map((group) => (
                  <div key={group.title}>
                    <div className="grp">{group.title}</div>
                    {group.entries.map((entry) => {
                      optionIndex += 1;
                      const index = optionIndex;
                      const selected = index === cursorIndex;
                      const disabled = entry.kind === 'cmd' && entry.disabled === true;
                      return (
                        <div
                          key={entry.key}
                          role="option"
                          aria-selected={selected}
                          aria-disabled={disabled || undefined}
                          title={entry.kind === 'cmd' ? entry.title : undefined}
                          data-testid={`command-palette-opt-${entry.key}`}
                          className={`opt${disabled ? ' disabled' : ''}`}
                          onClick={() => activate(entry)}
                        >
                          <Icon name={entry.icon} size={13} className="ic" />
                          {entry.kind === 'tab' ? (
                            <>
                              <span className="tt">{entry.title}</span>
                              <span className="tm">{entry.tm}</span>
                              <span
                                role="button"
                                tabIndex={-1}
                                aria-label={`关闭 ${entry.title}`}
                                title="关闭标签页"
                                className="x"
                                data-testid={`command-palette-close-${entry.key}`}
                                onClick={(event) => closeEntry(entry, event)}
                              >
                                ×
                              </span>
                            </>
                          ) : (
                            <>
                              <span className="tt">{entry.label}</span>
                              {entry.kbd !== undefined && <span className="kbd">{entry.kbd}</span>}
                            </>
                          )}
                        </div>
                      );
                    })}
                  </div>
                ))
              )}
            </div>
            <div className="foot">
              <span><span className="kbd">↑↓</span> 选择</span>
              <span><span className="kbd">Enter</span> 执行</span>
              <span><span className="kbd">Esc</span> 关闭</span>
              <span style={{ marginLeft: 'auto' }} className="mono">Ctrl K 唤起</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
