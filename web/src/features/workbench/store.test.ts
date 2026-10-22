/**
 * workbench 域三 store 单测（A4 §3.2 workbench/diff/terminal 三行）。
 *
 * 重点：workbenchStore 标签开放集合 + 宽度夹取（220-480）；diffStore 快照/裁决/逐文件
 * patch 缓存与既有 verdict 保留；terminalStore 行环形上限（2000 滚存计数）+ 生命周期。
 */
import { beforeEach, describe, expect, it } from 'vitest';

import {
  MAX_TERMINAL_LINES,
  PANEL_WIDTH_MAX,
  PANEL_WIDTH_MIN,
  tabIdOf,
  useDiffStore,
  useTerminalStore,
  useWorkbenchStore,
} from './store';
import type { DiffFileDetail, DiffReviewDetail } from '@contracts/rest';

import { makeDiffReview } from '../../tests/msw-handlers';

const READY = {
  reviewId: 'rev-1',
  sessionId: 's1',
  round: 2,
  baseline: { kind: 'accepted', ref: 'lcode-baseline/round-1', commit: 'e83c516' } as DiffReviewDetail['baseline'],
  files: [
    { path: 'README.md', change: 'modified', additions: 5, deletions: 0 },
    { path: 'hello.py', change: 'modified', additions: 12, deletions: 3 },
  ] as DiffReviewDetail['files'],
  additionsTotal: 17,
  deletionsTotal: 3,
  patchRef: '/api/v1/diff-reviews/rev-1',
  createdAt: '2026-10-17T09:32:00+00:00',
};

beforeEach(() => {
  useWorkbenchStore.getState().reset();
  useDiffStore.getState().reset();
  useTerminalStore.getState().reset();
});

describe('workbenchStore：标签与面板（唯一可跨 feature 调 action 的 UI 编排 store）', () => {
  it('openTab：同 kind:id 复用不重复，打开即激活并展开面板', () => {
    useWorkbenchStore.getState().openTab({ kind: 'review', id: 'rev-1', title: '改动审查' });
    useWorkbenchStore.getState().openTab({ kind: 'review', id: 'rev-1', title: '改动审查' });

    expect(useWorkbenchStore.getState().tabs).toHaveLength(1);
    expect(useWorkbenchStore.getState().activeTabId).toBe(tabIdOf('review', 'rev-1'));
    expect(useWorkbenchStore.getState().collapsed).toBe(false);
  });

  it('closeTab：关激活标签回退到末尾标签；全部关闭则收起', () => {
    useWorkbenchStore.getState().openTab({ kind: 'review', id: 'r1', title: 'A' });
    useWorkbenchStore.getState().openTab({ kind: 'terminal', id: 't1', title: '终端' });

    useWorkbenchStore.getState().closeTab(tabIdOf('terminal', 't1'));
    expect(useWorkbenchStore.getState().activeTabId).toBe(tabIdOf('review', 'r1'));

    useWorkbenchStore.getState().closeTab(tabIdOf('review', 'r1'));
    expect(useWorkbenchStore.getState().tabs).toHaveLength(0);
    expect(useWorkbenchStore.getState().collapsed).toBe(true);
  });

  it('setWidth 夹取到 [220, 480]', () => {
    expect(PANEL_WIDTH_MIN).toBe(220);
    expect(PANEL_WIDTH_MAX).toBe(480);

    useWorkbenchStore.getState().setCollapsed(false);
    useWorkbenchStore.getState().setWidth(100);
    expect(useWorkbenchStore.getState().width).toBe(220);
    useWorkbenchStore.getState().setWidth(999);
    expect(useWorkbenchStore.getState().width).toBe(480);
    useWorkbenchStore.getState().setWidth(360.6);
    expect(useWorkbenchStore.getState().width).toBe(361);
  });
});

describe('diffStore：review 快照 / 裁决 / patch 缓存', () => {
  it('upsertFromEvent 落快照；重复到达保留既有 verdict（不回退裁决态）', () => {
    useDiffStore.getState().upsertFromEvent(READY);
    expect(useDiffStore.getState().reviews.get('rev-1')).toMatchObject({
      round: 2,
      additionsTotal: 17,
      verdict: 'pending',
    });

    useDiffStore.getState().applyVerdict({ reviewId: 'rev-1', verdict: 'accepted', decidedBy: 'local' });
    useDiffStore.getState().upsertFromEvent(READY); // 重放同快照

    expect(useDiffStore.getState().reviews.get('rev-1')).toMatchObject({
      verdict: 'accepted',
      decidedBy: 'local',
    });
  });

  it('applyVerdict / cachePatch 缺前态是 no-op', () => {
    useDiffStore.getState().applyVerdict({ reviewId: 'ghost', verdict: 'rejected' });
    useDiffStore.getState().cachePatch('ghost', { path: 'x', change: 'modified', additions: 1, deletions: 0 });
    expect(useDiffStore.getState().reviews.size).toBe(0);
  });

  it('cachePatch 按文件路径缓存，不覆盖其它文件', () => {
    useDiffStore.getState().upsertFromEvent(READY);
    const patch: DiffFileDetail = {
      path: 'hello.py',
      change: 'modified',
      additions: 12,
      deletions: 3,
      patch: '@@ -1 +1 @@',
    };
    useDiffStore.getState().cachePatch('rev-1', patch);

    const review = useDiffStore.getState().reviews.get('rev-1');
    expect(review?.patches.get('hello.py')).toEqual(patch);
    expect(review?.patches.size).toBe(1);
    expect(review?.files).toHaveLength(2); // 摘要与 patch 缓存分离
  });
});

describe('terminalStore：exec 回显 / 生命周期', () => {
  it('appendLines：版本递增 + execOrder 保持到达序', () => {
    useTerminalStore.getState().appendLines('call_1', ['a', 'b']);
    useTerminalStore.getState().appendLines('call_2', ['c']);
    useTerminalStore.getState().appendLines('call_1', ['d']);

    const store = useTerminalStore.getState();
    expect(store.execOrder).toEqual(['call_1', 'call_2']);
    expect(store.execs.get('call_1')).toMatchObject({ lines: ['a', 'b', 'd'], version: 2 });
    expect(store.execs.get('call_2')).toMatchObject({ lines: ['c'], version: 1 });
  });

  it(`环形上限 ${MAX_TERMINAL_LINES} 行：超出滚存，旧行丢弃并计数`, () => {
    const lines = Array.from({ length: MAX_TERMINAL_LINES + 50 }, (_, i) => `line-${i}`);
    useTerminalStore.getState().appendLines('call_1', lines);

    const exec = useTerminalStore.getState().execs.get('call_1');
    expect(exec?.lines).toHaveLength(MAX_TERMINAL_LINES);
    expect(exec?.lines[0]).toBe('line-50'); // 前 50 行滚存
    expect(exec?.droppedLines).toBe(50);
    expect(exec?.lines.at(-1)).toBe(`line-${MAX_TERMINAL_LINES + 49}`);
  });

  it('setLifecycle：相位/提供方/原因 + sandboxId（null 不覆盖已有值）', () => {
    useTerminalStore.getState().setLifecycle({
      phase: 'creating',
      at: 't1',
      provider: 'bailian',
      reason: null,
      sandboxId: 'sbx_1',
    });
    expect(useTerminalStore.getState().lifecycle).toEqual({
      phase: 'creating',
      at: 't1',
      provider: 'bailian',
      reason: null,
    });

    useTerminalStore.getState().setLifecycle({ phase: 'paused', at: 't2', provider: 'bailian', reason: '空闲' });
    expect(useTerminalStore.getState().lifecycle).toMatchObject({ phase: 'paused', reason: '空闲' });
    expect(useTerminalStore.getState().sandboxId).toBe('sbx_1'); // 未带 sandboxId 时不覆盖
  });
});

// ---------------------------------------------------------------- M3 Phase 4 增量

describe('workbenchStore.openReviewTab（diff_ready 自动开标签语义）', () => {
  it('不存在时：新开 review 标签、激活并展开面板', () => {
    useWorkbenchStore.getState().setCollapsed(true);

    useWorkbenchStore.getState().openReviewTab('rev-1', '审查 · 第 1 轮');

    const state = useWorkbenchStore.getState();
    expect(state.tabs).toEqual([
      { id: tabIdOf('review', 'rev-1'), kind: 'review', title: '审查 · 第 1 轮' },
    ]);
    expect(state.activeTabId).toBe(tabIdOf('review', 'rev-1'));
    expect(state.collapsed).toBe(false);
  });

  it('已存在时：只同步标题，不重复开、不抢激活焦点、不强行展开', () => {
    useWorkbenchStore.getState().openReviewTab('rev-1', '审查 · 第 1 轮');
    // 用户切走并收起面板
    useWorkbenchStore.getState().openTab({ kind: 'terminal', id: 'main', title: '终端' });
    useWorkbenchStore.getState().setCollapsed(true);

    useWorkbenchStore.getState().openReviewTab('rev-1', '审查 · 第 2 轮');

    const state = useWorkbenchStore.getState();
    expect(state.tabs.filter((tab) => tab.id === tabIdOf('review', 'rev-1'))).toHaveLength(1);
    expect(state.activeTabId).toBe(tabIdOf('terminal', 'main'));
    expect(state.collapsed).toBe(true);
    expect(state.tabs.find((tab) => tab.id === tabIdOf('review', 'rev-1'))?.title).toBe(
      '审查 · 第 2 轮',
    );
  });
});

describe('diffStore.upsertFromRest（刷新恢复回填，终态不回退）', () => {
  it('store 无该 review：REST 全量回填（patchRef 按端点惯例合成）', () => {
    const detail = makeDiffReview({ id: 'rev-1', session_id: 'sess-a', verdict: 'pending' });

    useDiffStore.getState().upsertFromRest(detail);

    expect(useDiffStore.getState().reviews.get('rev-1')).toMatchObject({
      id: 'rev-1',
      sessionId: 'sess-a',
      round: detail.round,
      verdict: 'pending',
      feedback: null,
      decidedBy: null,
      patchRef: '/api/v1/diff-reviews/rev-1',
    });
  });

  it('本地终态 + REST 仍报 pending（陈旧快照）→ 终态不回退，patch 缓存保留', () => {
    useDiffStore.getState().upsertFromRest(makeDiffReview({ id: 'rev-1', verdict: 'pending' }));
    useDiffStore.getState().applyVerdict({
      reviewId: 'rev-1',
      verdict: 'accepted',
      feedback: null,
      decidedBy: 'user',
    });
    useDiffStore.getState().cachePatch('rev-1', {
      path: 'app.py',
      change: 'modified',
      additions: 1,
      deletions: 0,
      binary: false,
      patch: 'cached-patch',
      blob_ref: null,
    });

    useDiffStore.getState().upsertFromRest(makeDiffReview({ id: 'rev-1', verdict: 'pending' }));

    const review = useDiffStore.getState().reviews.get('rev-1');
    expect(review).toMatchObject({ verdict: 'accepted', decidedBy: 'user' });
    expect(review?.patches.get('app.py')?.patch).toBe('cached-patch');
  });

  it('本地 pending + REST 终态（错过 diff_resolved）→ 以 REST 为准', () => {
    useDiffStore.getState().upsertFromRest(makeDiffReview({ id: 'rev-1', verdict: 'pending' }));

    useDiffStore.getState().upsertFromRest(
      makeDiffReview({
        id: 'rev-1',
        verdict: 'rejected',
        feedback: '请补充测试',
        decided_by: 'user',
        resolved_at: '2026-10-18T00:00:10+00:00',
      }),
    );

    expect(useDiffStore.getState().reviews.get('rev-1')).toMatchObject({
      verdict: 'rejected',
      feedback: '请补充测试',
      decidedBy: 'user',
    });
  });
});

describe('diffStore.applyVerdict（feedback 单调合并，系统注记不被冲掉）', () => {
  const NOTE = '[系统] 返工未自动启动：会话已有运行中的任务。';

  it('回声（无注记）先到、响应（含注记）后到 → 注记保留', () => {
    useDiffStore.getState().upsertFromRest(makeDiffReview({ id: 'rev-1', verdict: 'pending' }));

    useDiffStore.getState().applyVerdict({
      reviewId: 'rev-1',
      verdict: 'rejected',
      feedback: '请补充测试',
      decidedBy: 'user',
    });
    useDiffStore.getState().applyVerdict({
      reviewId: 'rev-1',
      verdict: 'rejected',
      feedback: `请补充测试\n\n${NOTE}`,
      decidedBy: 'user',
    });

    expect(useDiffStore.getState().reviews.get('rev-1')?.feedback).toContain(NOTE);
  });

  it('响应（含注记）先到、回声（无注记）后到 → 注记仍保留', () => {
    useDiffStore.getState().upsertFromRest(makeDiffReview({ id: 'rev-1', verdict: 'pending' }));
    useDiffStore.getState().applyVerdict({
      reviewId: 'rev-1',
      verdict: 'rejected',
      feedback: `请补充测试\n\n${NOTE}`,
      decidedBy: 'user',
    });

    useDiffStore.getState().applyVerdict({
      reviewId: 'rev-1',
      verdict: 'rejected',
      feedback: '请补充测试',
      decidedBy: 'user',
    });

    expect(useDiffStore.getState().reviews.get('rev-1')?.feedback).toContain(NOTE);
  });

  it('accepted（feedback 为 null）不被合并逻辑污染', () => {
    useDiffStore.getState().upsertFromRest(makeDiffReview({ id: 'rev-1', verdict: 'pending' }));

    useDiffStore.getState().applyVerdict({
      reviewId: 'rev-1',
      verdict: 'accepted',
      feedback: null,
      decidedBy: 'user',
    });

    expect(useDiffStore.getState().reviews.get('rev-1')).toMatchObject({
      verdict: 'accepted',
      feedback: null,
      decidedBy: 'user',
    });
  });
});
