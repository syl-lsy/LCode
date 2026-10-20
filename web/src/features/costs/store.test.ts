/**
 * costStore 单测（A4 §3.2 costStore 行；REQ-015 / AC-015-3）。
 *
 * 重点：事件累计（sessionUsage 累加、缺省字段按 0）、REST 对账快照并存、
 * 三维汇总按维度缓存、沙箱计时基准。
 */
import { beforeEach, describe, expect, it } from 'vitest';

import { useCostStore } from './store';
import type { CostSummary, SessionCost } from '@contracts/rest';

const SESSION_COST: SessionCost = {
  model: { input_tokens: 210340, output_tokens: 38210 },
  model_cost: 1.86,
  sandbox: { accrued: 0.31, currency: 'CNY', rate_per_hour: 0.156 },
};

const SUMMARY_SESSION: CostSummary = {
  dimension: 'session',
  rows: [
    {
      group_key: 's1',
      model_calls: 42,
      input_tokens: 210340,
      output_tokens: 38210,
      total_tokens: 248550,
      cost: 1.86,
      currency: 'CNY',
    },
  ],
};

beforeEach(() => {
  useCostStore.getState().reset();
});

describe('costStore', () => {
  it('addUsage：事件用量累加（缺字段按 0；null/undefined 忽略）', () => {
    useCostStore.getState().addUsage({ input_tokens: 100, output_tokens: 50 });
    useCostStore.getState().addUsage({ input_tokens: 20 });
    useCostStore.getState().addUsage({});

    expect(useCostStore.getState().sessionUsage).toEqual({ input_tokens: 120, output_tokens: 50 });

    useCostStore.getState().addUsage(null);
    useCostStore.getState().addUsage(undefined);
    expect(useCostStore.getState().sessionUsage).toEqual({ input_tokens: 120, output_tokens: 50 });
  });

  it('setSessionUsage：REST 对账覆盖事件累计口径（并存字段，显示层择一）', () => {
    useCostStore.getState().addUsage({ input_tokens: 1, output_tokens: 1 });
    useCostStore.getState().setSessionUsage({ input_tokens: 210340, output_tokens: 38210 });
    expect(useCostStore.getState().sessionUsage).toEqual({ input_tokens: 210340, output_tokens: 38210 });
  });

  it('setSessionCost：REST 快照（模型段 + 沙箱段）', () => {
    useCostStore.getState().setSessionCost(SESSION_COST);
    expect(useCostStore.getState().sessionCost).toEqual(SESSION_COST);
  });

  it('setSummary：按维度缓存互不覆盖', () => {
    useCostStore.getState().setSummary(SUMMARY_SESSION);
    useCostStore.getState().setSummary({ dimension: 'date', rows: [] });

    const cache = useCostStore.getState().summaryCache;
    expect(cache.get('session')).toEqual(SUMMARY_SESSION);
    expect(cache.get('date')).toEqual({ dimension: 'date', rows: [] });
    expect(cache.size).toBe(2);
  });

  it('noteSandboxPhase：记录最近一次相位迁移时刻（§5.3 计时基准）', () => {
    useCostStore.getState().noteSandboxPhase('running', '2026-10-17T09:00:00+00:00');
    useCostStore.getState().noteSandboxPhase('paused', '2026-10-17T09:45:00+00:00');

    expect(useCostStore.getState().sandboxPhase).toEqual({
      phase: 'paused',
      at: '2026-10-17T09:45:00+00:00',
    });
  });
});
