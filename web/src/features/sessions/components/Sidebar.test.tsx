// @vitest-environment jsdom
/**
 * 会话左栏（test-plan §2.4 AC-004-3：SessionList.test.tsx::test_sort_by_last_active_and_search）：
 * 最近活跃降序 + 标题搜索 + 新建/切换/删除；运行中光点 / 等待确认徽标（F11）；
 * 相对时间格式（lib 内联；nowMs 注入单测）。
 */
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { describe, expect, it } from 'vitest';

import { makeSession, requestLog } from '../../../tests/msw-handlers';
import { server } from '../../../tests/test-utils';
import '../../../tests/test-utils';
import { usePaletteStore } from '@/stores/paletteStore';
import { useSessionStore } from '../store';

import { Sidebar } from './Sidebar';
import { formatRelativeTime } from './SessionItem';

/** Sidebar 需要 router（useNavigate）；挂在探针路由下断言导航。 */
function renderSidebar(): void {
  const router = createMemoryRouter(
    [
      { path: '/', element: <Sidebar /> },
      { path: '/s/:sessionId', element: <div data-testid="session-probe" /> },
    ],
    { initialEntries: ['/'] },
  );
  render(<RouterProvider router={router} />);
}

const baseTime = Date.parse('2026-10-18T03:00:00+00:00');

describe('formatRelativeTime', () => {
  it('刚刚 / N 分钟前 / 当日 HH:mm / 跨日 MM-DD HH:mm', () => {
    expect(formatRelativeTime('2026-10-18T02:59:30+00:00', baseTime)).toBe('刚刚');
    expect(formatRelativeTime('2026-10-18T02:35:00+00:00', baseTime)).toBe('25 分钟前');
    const sameDayNoon = Date.parse('2026-10-18T12:30:00');
    expect(formatRelativeTime(new Date(sameDayNoon).toISOString(), Date.parse('2026-10-18T14:00:00'))).toBe('12:30');
    expect(formatRelativeTime(new Date(Date.parse('2026-10-17T09:05:00')).toISOString(), Date.parse('2026-10-18T10:00:00'))).toMatch(/^10-17 \d{2}:05$/);
  });

  it('非法时间串返回空串（如实降级，不崩）', () => {
    expect(formatRelativeTime('not-a-date', baseTime)).toBe('');
  });
});

describe('Sidebar · 列表排序与搜索（AC-004-3）', () => {
  it('最近活跃降序：sess-c(10-18) → sess-b(10-17 12:00) → sess-a(10-17 08:00)', async () => {
    renderSidebar();
    await waitFor(() => {
      expect(screen.getAllByTestId('session-item')).toHaveLength(3);
    });
    const ids = screen.getAllByTestId('session-item').map((el) => el.getAttribute('data-session-id'));
    expect(ids).toEqual(['sess-c', 'sess-b', 'sess-a']);
    expect(screen.getByTestId('session-count')).toHaveTextContent('3');
  });

  it('搜索：标题子串过滤（大小写不敏感），无匹配显示空态', async () => {
    const user = userEvent.setup();
    renderSidebar();
    await waitFor(() => expect(screen.getAllByTestId('session-item')).toHaveLength(3));

    await user.type(screen.getByTestId('session-search'), '斐波');
    expect(screen.getAllByTestId('session-item')).toHaveLength(1);
    expect(screen.getByTestId('session-item')).toHaveAttribute('data-session-id', 'sess-b');
    expect(useSessionStore.getState().query).toBe('斐波');

    await user.clear(screen.getByTestId('session-search'));
    await user.type(screen.getByTestId('session-search'), '不存在的关键词');
    expect(screen.queryAllByTestId('session-item')).toHaveLength(0);
    expect(screen.getByTestId('empty-state')).toHaveTextContent('没有匹配的会话');
  });

  it('运行中光点与等待确认徽标（F11）按 run_state 呈现', async () => {
    renderSidebar();
    await waitFor(() => expect(screen.getAllByTestId('session-item')).toHaveLength(3));
    const itemsNow = screen.getAllByTestId('session-item');
    const running = itemsNow.find((el) => el.getAttribute('data-run-state') === 'running');
    if (running === undefined) throw new Error('无 running 条目');
    expect(running).toHaveAttribute('data-session-id', 'sess-b');
    expect(within(running).getByTestId('session-running-dot')).toBeInTheDocument();

    act(() => {
      useSessionStore.getState().setRunState('sess-c', 'waiting_approval');
    });
    await waitFor(() => {
      const waiting = screen
        .getAllByTestId('session-item')
        .find((el) => el.getAttribute('data-session-id') === 'sess-c');
      expect(waiting).toBeDefined();
      expect(waiting).toHaveAttribute('data-run-state', 'waiting_approval');
      expect(within(waiting as HTMLElement).getByTestId('session-await-badge')).toHaveTextContent('等待确认');
    });
  });

  it('新建会话：POST /sessions 后导航到 /s/{id}', async () => {
    const user = userEvent.setup();
    requestLog.length = 0;
    renderSidebar();
    await user.click(screen.getByTestId('new-session'));
    await waitFor(() => expect(screen.getByTestId('session-probe')).toBeInTheDocument());
    expect(requestLog.some((entry) => entry.method === 'POST' && entry.url.endsWith('/sessions'))).toBe(true);
    expect(useSessionStore.getState().list.some((s) => s.id === 'sess-new')).toBe(true);
  });

  it('切换会话：点击条目导航到 /s/{id} 并标记当前', async () => {
    const user = userEvent.setup();
    renderSidebar();
    await waitFor(() => expect(screen.getAllByTestId('session-item')).toHaveLength(3));
    const items = screen.getAllByTestId('session-item');
    const target = items.find((el) => el.getAttribute('data-session-id') === 'sess-a');
    if (target === undefined) throw new Error('sess-a 条目缺失');
    await user.click(within(target).getByTestId('session-open'));
    expect(screen.getByTestId('session-probe')).toBeInTheDocument();
  });

  it('删除：DELETE 后条目移除；删除当前会话 → 回首页', async () => {
    const user = userEvent.setup();
    requestLog.length = 0;
    renderSidebar();
    await waitFor(() => expect(screen.getAllByTestId('session-item')).toHaveLength(3));

    useSessionStore.getState().setCurrent('sess-c');
    const target = screen
      .getAllByTestId('session-item')
      .find((el) => el.getAttribute('data-session-id') === 'sess-c');
    if (target === undefined) throw new Error('sess-c 条目缺失');
    await user.click(within(target).getByTestId('session-delete'));

    await waitFor(() => {
      expect(screen.getAllByTestId('session-item')).toHaveLength(2);
    });
    expect(requestLog.some((entry) => entry.method === 'DELETE' && entry.url.includes('sess-c'))).toBe(true);
    expect(useSessionStore.getState().currentId).toBeNull();
    expect(useSessionStore.getState().list.some((s) => s.id === 'sess-c')).toBe(false);
  });

  it('标题兜底：空标题显示「新会话 · 时间」而不用空白条目（A4 §12 未决 10）', async () => {
    useSessionStore.getState().setList([
      makeSession({ id: 'sess-blank', title: '  ', last_active_at: '2026-10-18T02:59:50+00:00' }),
    ]);
    renderSidebar();
    expect(screen.getByTestId('session-open')).toHaveTextContent('新会话');
    await waitFor(() => expect(screen.getByTestId('session-count')).toHaveTextContent('1'));
  });
});

describe('Sidebar · 原型结构补全（B1：两视图工具条 + 底部导航项占位）', () => {
  it('视图 tab 切换两组工具条（F9/R3§B）：项目=收起全部/筛选和排序/归档；分组=新建分组/归档', async () => {
    const user = userEvent.setup();
    renderSidebar();
    await waitFor(() => expect(screen.getAllByTestId('session-item')).toHaveLength(3));

    // 默认项目视图（原型 aria-selected 口径）
    expect(screen.getByTestId('view-tab-proj')).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('toolbar-proj')).toBeVisible();
    expect(screen.getByTestId('toolbar-group')).not.toBeVisible();

    await user.click(screen.getByTestId('view-tab-group'));
    expect(screen.getByTestId('view-tab-group')).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('toolbar-group')).toBeVisible();
    expect(within(screen.getByTestId('toolbar-group')).getByTitle('新建分组')).toBeInTheDocument();
    expect(screen.getByTestId('toolbar-proj')).not.toBeVisible();
  });

  it('两视图工具条占位（收起全部/筛选和排序/新建分组）+ 底部导航项占位；归档钮为真实入口（F10，B6）', async () => {
    renderSidebar();
    await waitFor(() => expect(screen.getAllByTestId('session-item')).toHaveLength(3));

    const placeholders = [
      ['收起全部', 'toolbar-proj'],
      ['筛选和排序', 'toolbar-proj'],
      ['新建分组', 'toolbar-group'],
    ] as const;
    for (const [title, scope] of placeholders) {
      const btn = within(screen.getByTestId(scope)).getByTitle(title);
      expect(btn).toHaveAttribute('aria-disabled', 'true');
    }
    // 归档钮（两视图各一）已是真实入口：非占位，点击进归档模式（见下条用例）
    expect(within(screen.getByTestId('toolbar-proj')).getByTitle('归档')).not.toHaveAttribute('aria-disabled');

    // 侧栏底部：账号钮（F16 账号菜单触发器，头像 L + lcode）+ 移动端远程控制 + 设置
    const account = screen.getByTestId('account-button');
    expect(within(account).getByText('lcode')).toBeInTheDocument();
    for (const title of ['自动化（P2 占位）', '插件市场（P2 占位）', '移动端远程控制（P2 占位）', '设置（P1 占位）']) {
      expect(screen.getByTitle(title)).toHaveAttribute('aria-disabled', 'true');
    }
  });

  it('归档模式（F10/B6，empty-states #6）：工具条 = 筛选和排序｜归档操作[disabled]｜关闭 + 空态「暂无归档任务」', async () => {
    const user = userEvent.setup();
    renderSidebar();
    await waitFor(() => expect(screen.getAllByTestId('session-item')).toHaveLength(3));

    await user.click(within(screen.getByTestId('toolbar-proj')).getByTestId('archive-toggle'));
    const archive = screen.getByTestId('archive-mode');
    expect(archive).toHaveTextContent('暂无归档任务');
    expect(archive).toHaveTextContent('「归档操作」为批量操作入口，空态禁用。');
    // F10 工具条三件：筛选和排序（占位）｜归档操作（disabled，官方空态禁用）｜关闭（真实退出）
    expect(within(archive).getByTitle('筛选和排序（P1 占位）')).toHaveAttribute('aria-disabled', 'true');
    expect(within(archive).getByText('归档操作')).toBeDisabled();
    // 归档模式下视图 tab 与列表隐藏
    expect(screen.queryByTestId('view-tab-group')).not.toBeInTheDocument();
    expect(screen.queryByTestId('session-item')).not.toBeInTheDocument();

    await user.click(within(archive).getByTestId('archive-close'));
    expect(screen.queryByTestId('archive-mode')).not.toBeInTheDocument();
    expect(screen.getAllByTestId('session-item')).toHaveLength(3);
  });

  it('两视图空态（B6，empty-states #2/#3）：项目视图 = 暂无任务；分组视图 = 还没有任务 + 新建任务真实', async () => {
    const user = userEvent.setup();
    requestLog.length = 0;
    // 空列表：override 列表接口返回空页
    server.use(
      http.get('*/api/v1/sessions', () =>
        HttpResponse.json({ code: '0', message: 'ok', data: { items: [], total: 0 } }),
      ),
    );
    renderSidebar();
    await waitFor(() => expect(screen.getByTestId('session-list-empty')).toBeInTheDocument());

    // 默认项目视图（F9 口径）→「暂无任务 · 选中项目 root 后…」+ 添加项目占位
    const projEmpty = screen.getByTestId('session-list-empty');
    expect(projEmpty).toHaveTextContent('暂无任务');
    expect(projEmpty).toHaveTextContent('选中项目 root 后，该项目作用域内的任务会出现在这里。');
    expect(within(projEmpty).getByTitle('添加项目（P1 占位）')).toHaveAttribute('aria-disabled', 'true');

    // 分组视图 →「还没有任务」+ 新建任务钮（真实建会话）
    await user.click(screen.getByTestId('view-tab-group'));
    const groupEmpty = screen.getByTestId('session-list-empty');
    expect(groupEmpty).toHaveTextContent('还没有任务');
    expect(groupEmpty).toHaveTextContent('新建任务后，这里会按分组展示你的任务与状态。');
    await user.click(within(groupEmpty).getByTestId('empty-new-task'));
    await waitFor(() =>
      expect(requestLog.some((entry) => entry.method === 'POST' && entry.url.endsWith('/sessions'))).toBe(true),
    );
  });

  it('搜索钮打开 ⌘K 合并面板（F25：官方搜索钮实测指向标签页切换器）', async () => {
    const user = userEvent.setup();
    renderSidebar();
    await waitFor(() => expect(screen.getAllByTestId('session-item')).toHaveLength(3));
    expect(usePaletteStore.getState().open).toBe(false);
    await user.click(screen.getByTestId('sidebar-search'));
    expect(usePaletteStore.getState().open).toBe(true);
    usePaletteStore.getState().closePalette();
  });
});
