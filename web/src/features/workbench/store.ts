import { create } from 'zustand';

import type { DiffFileDetail, DiffReviewDetail } from '@contracts/rest';

/**
 * 右栏多用面板 store 三件套（A4 §3.2 workbench/diff/terminal 三行）。
 *
 * 为什么同文件：三个 store 同属 workbench 域（§3.2 表把 diffStore 记作
 * 「features/workbench 同级（review 域）」、terminalStore 记作
 * 「features/workbench（terminal 域）」）——同域三写者各自独立切片，放在同一模块
 * 便于 dispatcher（handlers.ts）一次接线，避免为拆目录再造 3 个小文件。
 *
 * workbenchStore 是「UI 编排 store」：其 action 允许被跨 feature 调用（§2.2 跨
 * feature 通信规则的唯一例外）；diff / terminal 是数据 store，仍封闭。
 */

export type TabKind = 'review' | 'terminal' | 'subagent' | 'placeholder';

export interface WorkbenchTab {
  readonly id: string;
  readonly kind: TabKind;
  readonly title: string;
}

export interface WorkbenchStore {
  tabs: WorkbenchTab[];
  activeTabId: string | null;
  /** 面板宽度（220-480px，R10；localStorage 持久化归 M2 任务 2） */
  width: number;
  collapsed: boolean;
  openTab: (tab: { kind: TabKind; id: string; title: string }) => void;
  closeTab: (tabId: string) => void;
  setActiveTab: (tabId: string | null) => void;
  setWidth: (width: number) => void;
  setCollapsed: (collapsed: boolean) => void;
  reset: () => void;
}

export const PANEL_WIDTH_MIN = 220;
export const PANEL_WIDTH_MAX = 480;
const PANEL_WIDTH_DEFAULT = 320;

/** 持久化键（A4 §9.1：键前缀 `lcode.`；width/collapsed 归本 store，§3.2）。 */
const PANEL_WIDTH_KEY = 'lcode.panel.width';
const PANEL_COLLAPSED_KEY = 'lcode.panel.collapsed';

function readStoredWidth(): number {
  try {
    const raw = localStorage.getItem(PANEL_WIDTH_KEY);
    if (raw === null) return PANEL_WIDTH_DEFAULT;
    const parsed = Number.parseInt(raw, 10);
    if (!Number.isFinite(parsed)) return PANEL_WIDTH_DEFAULT;
    return clampWidth(parsed);
  } catch {
    return PANEL_WIDTH_DEFAULT;
  }
}

function readStoredCollapsed(): boolean | null {
  try {
    const raw = localStorage.getItem(PANEL_COLLAPSED_KEY);
    if (raw === null) return null; // 缺键 → 保持默认（收起）
    return raw === '1';
  } catch {
    return null;
  }
}

function persistWidth(width: number): void {
  try {
    localStorage.setItem(PANEL_WIDTH_KEY, String(clampWidth(width)));
  } catch {
    // ignore：持久化失败不阻断交互
  }
}

function persistCollapsed(collapsed: boolean): void {
  try {
    localStorage.setItem(PANEL_COLLAPSED_KEY, collapsed ? '1' : '0');
  } catch {
    // ignore
  }
}

export function tabIdOf(kind: TabKind, id: string): string {
  return `${kind}:${id}`;
}

function clampWidth(width: number): number {
  return Math.min(PANEL_WIDTH_MAX, Math.max(PANEL_WIDTH_MIN, Math.round(width)));
}

interface WorkbenchData {
  tabs: WorkbenchTab[];
  activeTabId: string | null;
  width: number;
  collapsed: boolean;
}

function createWorkbenchSlice(): WorkbenchData {
  let width = PANEL_WIDTH_DEFAULT;
  let collapsed = true;
  try {
    width = readStoredWidth();
    const storedCollapsed = readStoredCollapsed();
    if (storedCollapsed !== null) collapsed = storedCollapsed;
  } catch {
    // 无 localStorage（SSR/Node 测试环境）：默认值
  }
  return { tabs: [], activeTabId: null, width, collapsed };
}

export const useWorkbenchStore = create<WorkbenchStore>((set) => ({
  ...createWorkbenchSlice(),
  openTab: ({ kind, id, title }) =>
    set((s) => {
      const tabId = tabIdOf(kind, id);
      const exists = s.tabs.some((tab) => tab.id === tabId);
      return {
        tabs: exists ? s.tabs : [...s.tabs, { id: tabId, kind, title }],
        activeTabId: tabId,
        collapsed: false,
      };
    }),
  closeTab: (tabId) =>
    set((s) => {
      const tabs = s.tabs.filter((tab) => tab.id !== tabId);
      const activeTabId =
        s.activeTabId === tabId ? (tabs.at(-1)?.id ?? null) : s.activeTabId;
      return { tabs, activeTabId, collapsed: tabs.length === 0 ? true : s.collapsed };
    }),
  setActiveTab: (tabId) => set({ activeTabId: tabId }),
  // 拖拽实时写 width（change 提交持久化，§9.1）；收起/展开持久化
  setWidth: (width) => {
    persistWidth(width);
    set({ width: clampWidth(width) });
  },
  setCollapsed: (collapsed) => {
    persistCollapsed(collapsed);
    set({ collapsed });
  },
  reset: () => set(createWorkbenchSlice()),
}));

// ---------------------------------------------------------------- diff（review 域）

/** diff 审查快照（diff_ready 全量 + verdict + 逐文件 patch 懒加载缓存，§3.2）。 */
export interface DiffReviewState {
  readonly id: string;
  readonly sessionId: string;
  readonly round: number;
  readonly baseline: DiffReviewDetail['baseline'];
  readonly files: DiffReviewDetail['files'];
  readonly additionsTotal: number;
  readonly deletionsTotal: number;
  readonly patchRef: string;
  readonly createdAt: string;
  readonly verdict: DiffReviewDetail['verdict'];
  readonly feedback: string | null;
  readonly decidedBy: string | null;
  /** 逐文件 patch 缓存（REST 懒加载，键=文件路径） */
  readonly patches: Map<string, DiffFileDetail>;
}

export interface DiffStore {
  reviews: Map<string, DiffReviewState>;
  upsertFromEvent: (input: {
    reviewId: string;
    sessionId: string;
    round: number;
    baseline: DiffReviewState['baseline'];
    files: DiffReviewState['files'];
    additionsTotal: number;
    deletionsTotal: number;
    patchRef: string;
    createdAt: string;
  }) => void;
  applyVerdict: (input: {
    reviewId: string;
    verdict: 'accepted' | 'rejected';
    feedback?: string | null;
    decidedBy?: string | null;
  }) => void;
  cachePatch: (reviewId: string, detail: DiffFileDetail) => void;
  reset: () => void;
}

interface DiffData {
  reviews: Map<string, DiffReviewState>;
}

function createDiffSlice(): DiffData {
  return { reviews: new Map() };
}

export const useDiffStore = create<DiffStore>((set) => ({
  ...createDiffSlice(),
  upsertFromEvent: (input) =>
    set((s) => {
      const reviews = new Map(s.reviews);
      const existing = reviews.get(input.reviewId);
      reviews.set(input.reviewId, {
        id: input.reviewId,
        sessionId: input.sessionId,
        round: input.round,
        baseline: input.baseline,
        files: input.files,
        additionsTotal: input.additionsTotal,
        deletionsTotal: input.deletionsTotal,
        patchRef: input.patchRef,
        createdAt: input.createdAt,
        verdict: existing?.verdict ?? 'pending',
        feedback: existing?.feedback ?? null,
        decidedBy: existing?.decidedBy ?? null,
        patches: existing?.patches ?? new Map(),
      });
      return { reviews };
    }),
  applyVerdict: ({ reviewId, verdict, feedback = null, decidedBy = null }) =>
    set((s) => {
      const existing = s.reviews.get(reviewId);
      if (!existing) return {};
      const reviews = new Map(s.reviews);
      reviews.set(reviewId, { ...existing, verdict, feedback, decidedBy });
      return { reviews };
    }),
  cachePatch: (reviewId, detail) =>
    set((s) => {
      const existing = s.reviews.get(reviewId);
      if (!existing) return {};
      const patches = new Map(existing.patches);
      patches.set(detail.path, detail);
      const reviews = new Map(s.reviews);
      reviews.set(reviewId, { ...existing, patches });
      return { reviews };
    }),
  reset: () => set(createDiffSlice()),
}));

// ---------------------------------------------------------------- terminal（terminal 域）

/** 单条 exec 回显（§3.2 terminalStore：execs + lifecycle + sandboxId）。 */
export interface TerminalExec {
  readonly execId: string;
  /** 已回显行（环形上限 MAX_TERMINAL_LINES，超出滚存——旧的丢、计数留痕） */
  readonly lines: string[];
  /** 因环形上限被滚存丢弃的行数（UI 可标注「已滚存 N 行」） */
  readonly droppedLines: number;
  readonly version: number;
}

export type SandboxPhase = NonNullable<import('@contracts/events').SandboxOutputEvent['phase']>;

export interface TerminalLifecycle {
  readonly phase: SandboxPhase;
  readonly at: string;
  readonly provider: NonNullable<import('@contracts/events').SandboxOutputEvent['provider']> | null;
  readonly reason: string | null;
}

export const MAX_TERMINAL_LINES = 2000;

export interface TerminalStore {
  execs: Map<string, TerminalExec>;
  /** exec 追加序（渲染顺序：命令来源标注 §6.3） */
  execOrder: string[];
  lifecycle: TerminalLifecycle | null;
  sandboxId: string | null;
  appendLines: (execId: string, lines: readonly string[]) => void;
  setLifecycle: (input: {
    phase: SandboxPhase;
    at: string;
    provider?: TerminalLifecycle['provider'];
    reason?: string | null;
    sandboxId?: string | null;
  }) => void;
  reset: () => void;
}

interface TerminalData {
  execs: Map<string, TerminalExec>;
  execOrder: string[];
  lifecycle: TerminalLifecycle | null;
  sandboxId: string | null;
}

function createTerminalSlice(): TerminalData {
  return { execs: new Map(), execOrder: [], lifecycle: null, sandboxId: null };
}

export const useTerminalStore = create<TerminalStore>((set) => ({
  ...createTerminalSlice(),
  appendLines: (execId, lines) =>
    set((s) => {
      if (lines.length === 0) return {};
      const existing = s.execs.get(execId);
      const merged = [...(existing?.lines ?? []), ...lines];
      const overflow = Math.max(0, merged.length - MAX_TERMINAL_LINES);
      const execs = new Map(s.execs);
      execs.set(execId, {
        execId,
        lines: overflow > 0 ? merged.slice(overflow) : merged,
        droppedLines: (existing?.droppedLines ?? 0) + overflow,
        version: (existing?.version ?? 0) + 1,
      });
      return {
        execs,
        execOrder: existing ? s.execOrder : [...s.execOrder, execId],
      };
    }),
  setLifecycle: ({ phase, at, provider = null, reason = null, sandboxId = null }) =>
    set((s) => ({
      lifecycle: { phase, at, provider, reason },
      sandboxId: sandboxId ?? s.sandboxId,
    })),
  reset: () => set(createTerminalSlice()),
}));
