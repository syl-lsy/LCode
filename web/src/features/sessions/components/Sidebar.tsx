import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router';

import { createSession, listSessions } from '@/api/sessions';
import { useSessionStore, selectVisibleSessions } from '../store';
import { usePaletteStore } from '@/stores/paletteStore';
import { useUiStore } from '@/stores/uiStore';

import { EmptyState } from '@/components/EmptyState';
import { Icon } from '@/components/Icon';
import { Spinner } from '@/components/Spinner';

import { SessionItem } from './SessionItem';
import { SessionSearch } from './SessionSearch';
import { SidebarToolbar } from './SidebarToolbar';
import { AccountMenu } from './AccountMenu';

/**
 * 左栏（A4 §8.1 Sidebar 行；AC-004-1/3；原型 app-shell 侧栏结构 1:1）。
 *
 * 结构（app-shell.html）：.side-top（新建任务 + 搜索 + 导航项 + 分组/项目视图
 * 工具条 F9）→ 搜索框/工具条/会话列表（LCode 功能区）→ .side-bottom（账号 +
 * 移动端远程控制 + 设置）。
 *
 * - 分组/项目两视图工具条联动（F9/R3§B）：分组 = 新建分组+归档；项目 =
 *   收起全部+筛选和排序+归档；本页按钮除「归档」外占位（aria-disabled + title）；
 * - 归档模式（F10/R3§B，B6）：「归档」钮进入——工具条变「筛选和排序｜归档操作
 *   [disabled]｜关闭」+ 空态「暂无归档任务」（empty-states #6；LCode 无归档
 *   API，空态恒真），「关闭」退出；
 * - 两视图空态（B6，empty-states #2/#3）：分组视图 = 「还没有任务」+ 新建任务钮；
 *   项目视图 = 「暂无任务 · 选中项目 root 后…」+ 添加项目（占位）；
 * - 搜索钮同指向 ⌘K 合并面板（F25：官方搜索钮实测打开标签页切换器）；
 * - 自动化/插件市场/账号/移动端远程控制/设置为 P1/P2 占位（禁用占位，样式对齐原型）；
 * - 列表事实源 = REST 首载（sessionStore.setList）；事件侧活跃度/运行徽标由 dispatcher
 *   更新（handlers.ts）。当前会话被删除 → 导航回首页（沙箱 kill 联动在服务端）。
 */
export function Sidebar() {
  const navigate = useNavigate();
  const list = useSessionStore((s) => s.list);
  const query = useSessionStore((s) => s.query);
  const currentId = useSessionStore((s) => s.currentId);
  const setList = useSessionStore((s) => s.setList);
  const setQuery = useSessionStore((s) => s.setQuery);
  const upsert = useSessionStore((s) => s.upsert);
  const remove = useSessionStore((s) => s.remove);
  const sidebarCollapsed = useUiStore((s) => s.sidebarCollapsed);
  const openPalette = usePaletteStore((s) => s.openPalette);

  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [loadError, setLoadError] = useState(false);
  /** F9 两视图（R3§B）：默认项目视图（原型 aria-selected 口径） */
  const [view, setView] = useState<'group' | 'proj'>('proj');
  /** F10 归档模式（B6）：进入后列表区替换为归档工具条 + 空态（无归档 API，恒空） */
  const [archiveMode, setArchiveMode] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);

  const reload = useCallback(async (): Promise<void> => {
    setLoading(true);
    setLoadError(false);
    try {
      const page = await listSessions();
      setList(page.items);
    } catch (error) {
      console.error('[sessions] 会话列表加载失败：', error);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, [setList]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const createNew = async (): Promise<void> => {
    if (creating) return;
    setCreating(true);
    try {
      const session = await createSession({});
      upsert(session);
      navigate(`/s/${session.id}`);
    } catch (error) {
      console.error('[sessions] 新建会话失败：', error);
    } finally {
      setCreating(false);
    }
  };

  const visible = selectVisibleSessions(list, query);

  const onDeleted = (id: string): void => {
    remove(id);
    if (currentId === id) navigate('/');
  };

  if (sidebarCollapsed) {
    return (
      <aside
        data-testid="sidebar-collapsed"
        className="flex h-full w-0 flex-col overflow-hidden border-r-0"
        aria-hidden="true"
      />
    );
  }

  return (
    /* .sidebar（components.css，来源 app-shell.html）：固定 264px、flex:none、
       侧栏底色 + 右描边；折叠仍走条件渲染（w-0 隐藏 aside），行为不变 */
    <aside data-testid="sidebar" className="sidebar h-full overflow-hidden">
      <div className="side-top">
        <button
          type="button"
          data-testid="new-session"
          disabled={creating}
          className="new-task"
          onClick={() => void createNew()}
        >
          {creating ? <Spinner label="正在新建会话" /> : <Icon name="plus" size={14} />}
          新建任务 <span className="kbd">Ctrl N</span>
        </button>
        <button
          type="button"
          data-testid="sidebar-search"
          className="side-item"
          title="搜索（Ctrl K · ⌘K 合并面板：打开的标签页 + 命令建议）"
          onClick={openPalette}
        >
          <Icon name="search" size={14} />
          搜索 <span className="kbd">Ctrl K</span>
        </button>
        <button type="button" className="side-item" aria-disabled="true" title="自动化（P2 占位）">
          <Icon name="clock" size={14} />
          自动化
        </button>
        <button type="button" className="side-item" aria-disabled="true" title="插件市场（P2 占位）">
          <Icon name="package" size={14} />
          插件市场
        </button>
        {/* F9 两视图工具条联动（R3§B）：分组/项目两态工具条不同；归档模式（F10）下整组隐藏 */}
        {!archiveMode && (
          <>
            <div className="view-tabs" role="tablist" aria-label="视图">
              <button
                type="button"
                role="tab"
                aria-selected={view === 'group'}
                data-testid="view-tab-group"
                onClick={() => setView('group')}
              >
                分组
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={view === 'proj'}
                data-testid="view-tab-proj"
                onClick={() => setView('proj')}
              >
                项目
              </button>
            </div>
            {/* F9 · 项目视图工具条：收起全部 + 筛选和排序 + 归档 */}
            <div className="tool-row" data-testid="toolbar-proj" hidden={view !== 'proj'}>
              <button type="button" className="icon-btn" aria-disabled="true" title="收起全部">
                <Icon name="collapse" size={14} />
              </button>
              <button type="button" className="icon-btn" aria-disabled="true" title="筛选和排序">
                <Icon name="filter" size={14} />
              </button>
              <button
                type="button"
                className="icon-btn"
                data-testid="archive-toggle"
                title="归档"
                onClick={() => setArchiveMode(true)}
              >
                <Icon name="archive" size={14} />
              </button>
            </div>
            {/* F9 · 分组视图工具条：新建分组 + 归档（无收起全部与筛选排序，R3§B） */}
            <div className="tool-row" data-testid="toolbar-group" hidden={view !== 'group'}>
              <button type="button" className="icon-btn" aria-disabled="true" title="新建分组">
                <Icon name="folder-plus" size={14} />
              </button>
              <button
                type="button"
                className="icon-btn"
                data-testid="archive-toggle-group"
                title="归档"
                onClick={() => setArchiveMode(true)}
              >
                <Icon name="archive" size={14} />
              </button>
            </div>
          </>
        )}
      </div>
      {/* F10 归档模式（B6，empty-states #6）：工具条 = 筛选和排序｜归档操作[disabled]｜关闭
          + 空态「暂无归档任务」（LCode 无归档 API，空态恒真） */}
      {archiveMode ? (
        <div data-testid="archive-mode" className="scroll-thin min-h-0 flex-1 overflow-y-auto px-2 pb-3">
          <EmptyState
            testId="archive-empty"
            icon={<Icon name="archive" size={16} />}
            title="暂无归档任务"
            description="在侧边栏进入归档模式后，这里列出已归档任务；「归档操作」为批量操作入口，空态禁用。"
            head={
              <div className="frame-tools">
                <button type="button" className="tb" aria-disabled="true" title="筛选和排序（P1 占位）">
                  <Icon name="filter" size={11} /> 筛选和排序
                </button>
                <button type="button" className="tb" disabled title="批量操作入口，空态禁用（官方行为）">
                  归档操作
                </button>
                <span className="grow" />
                <button type="button" className="tb" data-testid="archive-close" onClick={() => setArchiveMode(false)}>
                  关闭
                </button>
              </div>
            }
          />
        </div>
      ) : (
        <>
          <SessionSearch inputRef={searchRef} value={query} onChange={setQuery} />
          <SidebarToolbar count={visible.length} onRefresh={() => void reload()} refreshing={loading} />
          <div data-testid="session-list" className="scroll-thin min-h-0 flex-1 overflow-y-auto px-2 pb-3">
            {loadError && (
              <p className="px-2 py-3 text-[length:var(--text-ui-xs)] text-destructive" role="alert">
                会话列表加载失败，点击刷新按钮重试
              </p>
            )}
            {!loadError && visible.length === 0 && !loading && query.trim() !== '' && (
              <EmptyState
                title="没有匹配的会话"
                description="换个关键词试试。"
              />
            )}
            {/* B6 两视图空态（empty-states #2/#3）：分组 = 任务区空列表（新建任务真实）；
                项目 = 项目空列表（添加项目占位）。文案逐字照原型。 */}
            {!loadError && visible.length === 0 && !loading && query.trim() === '' && view === 'group' && (
              <EmptyState
                testId="session-list-empty"
                icon={<Icon name="list" size={16} />}
                title="还没有任务"
                description="新建任务后，这里会按分组展示你的任务与状态。"
                head={
                  <>
                    <span className="t">任务</span>
                    <span style={{ flex: 1 }} />
                    <button
                      type="button"
                      className="icon-btn"
                      style={{ width: 22, height: 22 }}
                      data-testid="empty-new-task"
                      title="新建任务"
                      disabled={creating}
                      onClick={() => void createNew()}
                    >
                      <Icon name="plus" size={11} />
                    </button>
                  </>
                }
              />
            )}
            {!loadError && visible.length === 0 && !loading && query.trim() === '' && view === 'proj' && (
              <EmptyState
                testId="session-list-empty"
                icon={<Icon name="folder" size={16} />}
                title="暂无任务"
                description="选中项目 root 后，该项目作用域内的任务会出现在这里。"
                head={
                  <>
                    <span className="t">项目</span>
                    <span style={{ flex: 1 }} />
                    <button
                      type="button"
                      className="icon-btn"
                      style={{ width: 22, height: 22 }}
                      aria-disabled="true"
                      title="添加项目（P1 占位）"
                    >
                      <Icon name="plus" size={11} />
                    </button>
                  </>
                }
              />
            )}
            {visible.map((session) => (
              <SessionItem
                key={session.id}
                session={session}
                active={session.id === currentId}
                onDeleted={onDeleted}
              />
            ))}
          </div>
        </>
      )}
      <div className="side-bottom">
        {/* F16 账号菜单（toolbar-menus 菜单 8，B5）：头像 + 用户名 + ID 复制 + 快捷子菜单 */}
        <AccountMenu />
        {/* F19 移动端远程控制：P2 占位 */}
        <button type="button" className="side-item" aria-disabled="true" title="移动端远程控制（P2 占位）">
          <Icon name="mobile" size={14} />
          移动端远程控制
        </button>
        <button type="button" className="side-item" aria-disabled="true" title="设置（P1 占位）">
          <Icon name="settings" size={14} />
          设置
        </button>
      </div>
    </aside>
  );
}
