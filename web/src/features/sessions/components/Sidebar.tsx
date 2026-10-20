import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router';

import { createSession, listSessions } from '@/api/sessions';
import { useSessionStore, selectVisibleSessions } from '../store';
import { useUiStore } from '@/stores/uiStore';

import { EmptyState } from '@/components/EmptyState';
import { Spinner } from '@/components/Spinner';

import { SessionItem } from './SessionItem';
import { SessionSearch } from './SessionSearch';
import { SidebarToolbar } from './SidebarToolbar';

/**
 * 左栏（A4 §8.1 Sidebar 行；AC-004-1/3）：新建 / 搜索 / 最近活跃排序 / 切换 / 删除。
 *
 * 列表事实源 = REST 首载（sessionStore.setList）；事件侧活跃度/运行徽标由 dispatcher
 * 更新（handlers.ts）。当前会话被删除 → 导航回首页（沙箱 kill 联动在服务端）。
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

  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [loadError, setLoadError] = useState(false);

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
    <aside
      data-testid="sidebar"
      className="flex h-full min-w-0 flex-col overflow-hidden border-r border-border bg-sidebar"
    >
      <div className="px-2.5 pt-2.5">
        <button
          type="button"
          data-testid="new-session"
          disabled={creating}
          className="flex h-8 w-full items-center gap-2 rounded-md border border-border bg-card px-2.5 text-[length:var(--text-ui-sm)] font-medium text-foreground-strong hover:border-border-hover hover:bg-surface-hover disabled:opacity-45"
          onClick={() => void createNew()}
        >
          {creating ? <Spinner label="正在新建会话" /> : <span aria-hidden="true">＋</span>}
          新建会话
        </button>
      </div>
      <div className="pt-1.5">
        <SessionSearch value={query} onChange={setQuery} />
      </div>
      <SidebarToolbar count={visible.length} onRefresh={() => void reload()} refreshing={loading} />
      <div data-testid="session-list" className="scroll-thin min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {loadError && (
          <p className="px-2 py-3 text-[length:var(--text-ui-xs)] text-destructive" role="alert">
            会话列表加载失败，点击 ⟳ 重试
          </p>
        )}
        {!loadError && visible.length === 0 && !loading && (
          <EmptyState
            title={query.trim() === '' ? '还没有会话' : '没有匹配的会话'}
            description={
              query.trim() === ''
                ? '点击「新建会话」开始第一次对话。'
                : '换个关键词试试。'
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
    </aside>
  );
}
