import { create } from 'zustand';

import type { SessionDetail } from '@contracts/rest';

/**
 * sessionStore：左栏会话列表（A4 §3.2 sessionStore 行 / §8.1）。
 *
 * 写入者：REST 首载（api/sessions.ts 调用方）与 dispatcher（活跃时间戳/运行徽标，
 * §5.3 里由 message_created / run_status / approval_* 事件驱动）。
 * 事件侧只更新**已在列表中**的会话（列表事实源是 REST；事件不带 title 等字段，
 * 凭空造条目会伪造不存在的会话）。
 */
export interface SessionStore {
  list: SessionDetail[];
  /** 搜索词（会话标题子串，AC-004-3） */
  query: string;
  currentId: string | null;
  setList: (items: readonly SessionDetail[]) => void;
  setQuery: (query: string) => void;
  setCurrent: (id: string | null) => void;
  upsert: (session: SessionDetail) => void;
  remove: (id: string) => void;
  touchActivity: (id: string, at: string) => void;
  setRunState: (id: string, runState: SessionDetail['run_state']) => void;
  reset: () => void;
}

interface SessionData {
  list: SessionDetail[];
  query: string;
  currentId: string | null;
}

function createSessionSlice(): SessionData {
  return { list: [], query: '', currentId: null };
}

/** 事件驱动的局部更新：仅命中已加载的会话（否则原样返回，不造条目）。 */
function patchSession(
  list: readonly SessionDetail[],
  id: string,
  patch: (session: SessionDetail) => SessionDetail,
): SessionDetail[] {
  let changed = false;
  const next = list.map((session) => {
    if (session.id !== id) return session;
    changed = true;
    return patch(session);
  });
  return changed ? next : (list as SessionDetail[]);
}

export const useSessionStore = create<SessionStore>((set) => ({
  ...createSessionSlice(),
  setList: (items) => set({ list: [...items] }),
  setQuery: (query) => set({ query }),
  setCurrent: (id) => set({ currentId: id }),
  upsert: (session) =>
    set((s) => {
      const exists = s.list.some((item) => item.id === session.id);
      return { list: exists ? s.list.map((item) => (item.id === session.id ? session : item)) : [session, ...s.list] };
    }),
  remove: (id) =>
    set((s) => ({
      list: s.list.filter((item) => item.id !== id),
      currentId: s.currentId === id ? null : s.currentId,
    })),
  touchActivity: (id, at) =>
    set((s) => ({
      list: patchSession(s.list, id, (session) =>
        session.last_active_at >= at ? session : { ...session, last_active_at: at },
      ),
    })),
  setRunState: (id, runState) =>
    set((s) => ({
      list: patchSession(s.list, id, (session) =>
        session.run_state === runState ? session : { ...session, run_state: runState },
      ),
    })),
  reset: () => set(createSessionSlice()),
}));

/**
 * 可见列表（纯函数 selector，AC-004-3）：标题子串过滤 + 最近活跃降序。
 * 排序以 `last_active_at` 降序稳定排列（时间戳同值时保持 REST 到达序）。
 */
export function selectVisibleSessions(
  list: readonly SessionDetail[],
  query: string,
): SessionDetail[] {
  const keyword = query.trim().toLowerCase();
  const filtered =
    keyword === ''
      ? [...list]
      : list.filter((session) => session.title.toLowerCase().includes(keyword));
  return filtered.sort((a, b) => (a.last_active_at < b.last_active_at ? 1 : a.last_active_at > b.last_active_at ? -1 : 0));
}
