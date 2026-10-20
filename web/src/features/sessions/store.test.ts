/**
 * sessionStore 单测（A4 §3.2 sessionsStore 行；AC-004-3 排序/搜索）。
 *
 * 重点：事件侧只更新**已加载**会话（不凭空造条目）；搜索为标题子串；
 * 最近活跃降序（同值保持到达序）。
 */
import { beforeEach, describe, expect, it } from 'vitest';

import { selectVisibleSessions, useSessionStore } from './store';
import type { SessionDetail } from '@contracts/rest';

function session(overrides: Partial<SessionDetail> = {}): SessionDetail {
  return {
    id: 's1',
    title: '修 issue #42 登录超时',
    status: 'active',
    model: 'qwen3-coder-plus',
    reasoning_effort: 'medium',
    approval_mode: 'manual',
    sandbox_provider: 'bailian',
    run_state: 'idle',
    created_at: '2026-10-17T09:00:00+00:00',
    updated_at: '2026-10-17T09:00:00+00:00',
    last_active_at: '2026-10-17T09:00:00+00:00',
    ...overrides,
  };
}

beforeEach(() => {
  useSessionStore.getState().reset();
});

describe('sessionStore：列表与选中', () => {
  it('setList / setCurrent / upsert / remove', () => {
    useSessionStore.getState().setList([session(), session({ id: 's2', title: '另一会话' })]);
    useSessionStore.getState().setCurrent('s1');
    expect(useSessionStore.getState().currentId).toBe('s1');

    useSessionStore.getState().upsert(session({ id: 's1', title: '改名后的会话' }));
    expect(useSessionStore.getState().list.map((item) => item.title)).toEqual([
      '改名后的会话',
      '另一会话',
    ]);
    expect(useSessionStore.getState().list).toHaveLength(2); // upsert 不新增

    useSessionStore.getState().remove('s1');
    expect(useSessionStore.getState().list.map((item) => item.id)).toEqual(['s2']);
    expect(useSessionStore.getState().currentId).toBeNull(); // 删除当前会话清选中
  });

  it('touchActivity / setRunState 只命中已加载会话（事件不凭空造条目）', () => {
    useSessionStore.getState().setList([session()]);

    useSessionStore.getState().touchActivity('s1', '2026-10-17T10:00:00+00:00');
    useSessionStore.getState().setRunState('s1', 'running');
    expect(useSessionStore.getState().list[0]).toMatchObject({
      last_active_at: '2026-10-17T10:00:00+00:00',
      run_state: 'running',
    });

    useSessionStore.getState().touchActivity('ghost', '2026-10-17T11:00:00+00:00');
    useSessionStore.getState().setRunState('ghost', 'idle');
    expect(useSessionStore.getState().list).toHaveLength(1);
  });

  it('touchActivity 不回退活跃时间（乱序事件到达时保持最新值）', () => {
    useSessionStore.getState().setList([session({ last_active_at: '2026-10-17T12:00:00+00:00' })]);
    useSessionStore.getState().touchActivity('s1', '2026-10-17T10:00:00+00:00');
    expect(useSessionStore.getState().list[0]?.last_active_at).toBe('2026-10-17T12:00:00+00:00');
  });
});

describe('selectVisibleSessions（AC-004-3）', () => {
  const list = [
    session({ id: 'a', title: '修复登录超时', last_active_at: '2026-10-17T09:00:00+00:00' }),
    session({ id: 'b', title: '重构 SSE 解析', last_active_at: '2026-10-17T11:00:00+00:00' }),
    session({ id: 'c', title: '登录页样式', last_active_at: '2026-10-17T10:00:00+00:00' }),
  ];

  it('空搜索 → 最近活跃降序', () => {
    expect(selectVisibleSessions(list, '').map((item) => item.id)).toEqual(['b', 'c', 'a']);
  });

  it('标题子串搜索（大小写不敏感）后仍按活跃降序', () => {
    expect(selectVisibleSessions(list, '登录').map((item) => item.id)).toEqual(['c', 'a']);
    expect(selectVisibleSessions(list, '  SSE  ').map((item) => item.id)).toEqual(['b']);
    expect(selectVisibleSessions(list, '不存在')).toEqual([]);
  });
});
