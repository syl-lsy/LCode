import { create } from 'zustand';

import type { SandboxOutputEvent } from '@contracts/events';
import type { Usage } from '@contracts/common';
import type { CostSummary, SessionCost } from '@contracts/rest';

/**
 * costStore：事件累计用量 + REST 对账快照（A4 §3.2 costStore 行 / §5.3）。
 *
 * 口径（AC-015-3）：
 * - `sessionUsage`：run_status / message_finished 事件的用量**累计**（事件侧实时可见）；
 * - `sessionCost`：GET /sessions/{id}/costs 的**对账**快照（含沙箱费用估算）——
 *   与事件累计并存而非互斥：事件是主数据源，REST 负责补齐沙箱段与校准；
 * - `summaryCache`：三维汇总（session/date/model）按维度缓存；
 * - `sandboxPhase`：sandbox_output(lifecycle) 的相位迁移时刻 = 沙箱计时基准（§5.3）。
 */
export type SandboxPhase = NonNullable<SandboxOutputEvent['phase']>;
export type CostDimension = CostSummary['dimension'];

export interface SandboxPhaseMark {
  readonly phase: SandboxPhase;
  readonly at: string;
}

export interface CostStore {
  sessionUsage: Usage;
  sessionCost: SessionCost | null;
  summaryCache: Map<CostDimension, CostSummary>;
  sandboxPhase: SandboxPhaseMark | null;
  addUsage: (usage: Usage | null | undefined) => void;
  setSessionUsage: (usage: Usage) => void;
  setSessionCost: (cost: SessionCost) => void;
  setSummary: (summary: CostSummary) => void;
  noteSandboxPhase: (phase: SandboxPhase, at: string) => void;
  reset: () => void;
}

interface CostData {
  sessionUsage: Usage;
  sessionCost: SessionCost | null;
  summaryCache: Map<CostDimension, CostSummary>;
  sandboxPhase: SandboxPhaseMark | null;
}

function createCostSlice(): CostData {
  return {
    sessionUsage: { input_tokens: 0, output_tokens: 0 },
    sessionCost: null,
    summaryCache: new Map(),
    sandboxPhase: null,
  };
}

export const useCostStore = create<CostStore>((set) => ({
  ...createCostSlice(),
  addUsage: (usage) =>
    set((s) => {
      if (!usage) return {};
      return {
        sessionUsage: {
          input_tokens: (s.sessionUsage.input_tokens ?? 0) + (usage.input_tokens ?? 0),
          output_tokens: (s.sessionUsage.output_tokens ?? 0) + (usage.output_tokens ?? 0),
        },
      };
    }),
  setSessionUsage: (usage) => set({ sessionUsage: { ...usage } }),
  setSessionCost: (cost) => set({ sessionCost: cost }),
  setSummary: (summary) =>
    set((s) => {
      const summaryCache = new Map(s.summaryCache);
      summaryCache.set(summary.dimension, summary);
      return { summaryCache };
    }),
  noteSandboxPhase: (phase, at) => set({ sandboxPhase: { phase, at } }),
  reset: () => set(createCostSlice()),
}));
