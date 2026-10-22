import { create } from 'zustand';

import type { AgentRef } from '@contracts/common';
import type {
  MessageFinishedEvent,
  RunStatusEvent,
  ToolCallFinishedEvent,
  ToolCallStartedEvent,
} from '@contracts/events';
import type { ApprovalItem } from '@contracts/rest';

/**
 * chatStore：消息流条目顺序表 + 流式 parts + run 状态（A4 §3.2 / §3.3 / §8.2）。
 *
 * 写入者唯一 = dispatcher（EVENTS/handlers.ts）；组件只按 selector 读。
 *
 * 不可变边界（§3.3）：
 * - `items` append-only（push 后换引用）；
 * - `parts` 只替换 `{text, version}` 对象引用；
 * - `byId` 条目仅在对应事件到达时替换引用；禁止深层可变结构。
 *
 * `generation`（§3.3 batch 路径）：重放/快照替换属「非追加输入」——batch 应用后自增，
 * 让消费组件的渲染缓存整体失效（streaming-buffer 规则 18）。
 *
 * 事件侧类型一律经 indexed access 从生成物取（ADR-003 契约单源，禁止手写 wire 形状）；
 * 本文件的类型全部是**本地 UI 形态**，不是 wire 形状。
 */

/** 事件侧别名（indexed access，勿手写）。 */
export type AgentEnvelope = AgentRef;
export type FinishReason = MessageFinishedEvent['finish_reason'];
export type StructuredPayload = NonNullable<MessageFinishedEvent['structured']>;
export type ToolErrorPayload = NonNullable<ToolCallFinishedEvent['error']>;
export type RunErrorPayload = NonNullable<RunStatusEvent['error']>;
export type ToolRiskLevel = NonNullable<ToolCallStartedEvent['risk_level']>;
export type ApprovalStatus = ApprovalItem['status'];

/** 流式渲染单元 kind：正文 / 思考（§6.1 两通道）。 */
export type PartKind = 'text' | 'thinking';

/** partKey = `${message_id}:${kind}`（§3.2）。 */
export function partKeyOf(messageId: string, kind: PartKind = 'text'): string {
  return `${messageId}:${kind}`;
}

/** 流式渲染单元：每次 drain 只换对象引用，version 驱动 selector 精确订阅（§3.3）。 */
export interface ChatPart {
  readonly text: string;
  readonly version: number;
}

export interface ChatMessageItem {
  readonly kind: 'message';
  readonly id: string;
  readonly role: 'user' | 'assistant';
  readonly createdAt: string;
  readonly runId: string | null;
  readonly agent: AgentEnvelope | null;
  /** user 消息正文；assistant 正文本体在 `parts`（partKeyOf(id)） */
  readonly content?: string;
  readonly finishReason?: FinishReason;
  readonly structured?: StructuredPayload | null;
}

export interface ChatToolCardItem {
  readonly kind: 'toolcard';
  readonly id: string;
  readonly tool: string;
  readonly args: Readonly<Record<string, unknown>>;
  /** running → success/error（AC-003-2 两态卡片；非 0 exit_code 仍是 success，读 exit_code 判断） */
  readonly status: 'running' | 'success' | 'error';
  readonly riskLevel: ToolRiskLevel | null;
  readonly agent: AgentEnvelope | null;
  readonly createdAt: string;
  readonly durationMs?: number;
  readonly error?: ToolErrorPayload | null;
  readonly exitCode?: number | null;
  readonly resultPreview?: string | null;
  readonly resultRef?: string | null;
  readonly truncated?: boolean;
}

export interface ChatApprovalItem {
  readonly kind: 'approval';
  readonly id: string;
  readonly tool: string;
  readonly status: ApprovalStatus;
  readonly createdAt: string;
}

export interface ChatDiffCardItem {
  readonly kind: 'diffcard';
  readonly id: string;
  readonly round: number;
  readonly fileCount: number;
  readonly additionsTotal: number;
  readonly deletionsTotal: number;
  readonly verdict: 'pending' | 'accepted' | 'rejected';
  readonly createdAt: string;
}

/** 子智能体卡片（§8.2 条目族；M3/M4 产出，M2 只保留形态） */
export interface ChatSubagentItem {
  readonly kind: 'subagent';
  readonly id: string;
  readonly agent: AgentEnvelope;
  readonly label: string;
  readonly createdAt: string;
}

/** /compact 状态行条目（AC-010-1 两态；subject=compact） */
export interface ChatCompactNoteItem {
  readonly kind: 'compact_note';
  readonly id: string;
  readonly phase: 'started' | 'finished';
  readonly beforeTokens: number | null;
  readonly afterTokens: number | null;
  readonly updatedAt: string;
}

export type ChatItem =
  | ChatMessageItem
  | ChatToolCardItem
  | ChatApprovalItem
  | ChatDiffCardItem
  | ChatSubagentItem
  | ChatCompactNoteItem;

export type ActiveRunState = 'idle' | 'running' | 'waiting_approval';

export interface ActiveRun {
  readonly runId: string | null;
  readonly state: ActiveRunState;
  readonly startedAt: string | null;
  /** run 终态错误对象（error 与 finished/stopped 同为终态；收敛时保留，UI 可读） */
  readonly error: RunErrorPayload | null;
}

export interface ChatStore {
  items: string[];
  byId: Map<string, ChatItem>;
  parts: Map<string, ChatPart>;
  activeRun: ActiveRun;
  /** 当前 /compact 状态行条目 id（started 建、finished 原地更新，避免重复条目） */
  compactItemId: string | null;
  /** 非追加输入的代际计数（batch 重放后自增，使渲染缓存全失效，§3.3） */
  generation: number;

  appendUserMessage: (input: {
    id: string;
    content: string;
    createdAt: string;
    runId: string | null;
    agent?: AgentEnvelope | null;
  }) => void;
  ensureAssistantMessage: (input: {
    id: string;
    createdAt: string;
    runId: string | null;
    agent?: AgentEnvelope | null;
  }) => void;
  finishMessage: (input: {
    id: string;
    finishReason: FinishReason;
    structured?: StructuredPayload | null;
  }) => void;
  appendToolCard: (input: {
    id: string;
    tool: string;
    args: Readonly<Record<string, unknown>>;
    riskLevel?: ToolRiskLevel | null;
    agent?: AgentEnvelope | null;
    createdAt: string;
  }) => void;
  completeToolCall: (input: {
    id: string;
    status: 'success' | 'error';
    durationMs: number;
    error?: ToolErrorPayload | null;
    exitCode?: number | null;
    resultPreview?: string | null;
    resultRef?: string | null;
    truncated?: boolean;
  }) => void;
  appendApprovalItem: (input: {
    id: string;
    tool: string;
    status: ApprovalStatus;
    createdAt: string;
  }) => void;
  setApprovalItemStatus: (id: string, status: ApprovalStatus) => void;
  appendDiffCard: (input: {
    id: string;
    round: number;
    fileCount: number;
    additionsTotal: number;
    deletionsTotal: number;
    createdAt: string;
  }) => void;
  setDiffCardVerdict: (id: string, verdict: 'accepted' | 'rejected') => void;
  beginCompact: (input: { id: string; at: string }) => void;
  finishCompact: (input: {
    id: string;
    at: string;
    beforeTokens: number | null;
    afterTokens: number | null;
  }) => void;
  applyRunStatus: (input: {
    subject: RunStatusEvent['subject'];
    phase: RunStatusEvent['phase'];
    runId: string | null;
    createdAt: string;
    error?: RunErrorPayload | null;
  }) => void;
  noteApprovalPending: () => void;
  /**
   * 审批批次守门（D3 batched interrupt）：只有 `allSettled`（该批挂起全部解决）
   * 为 true 时才把 waiting_approval 清回 running；批次未清时保持等待态
   * （Composer 不可输入、头部徽标不消失）。
   */
  noteApprovalSettled: (allSettled: boolean) => void;
  /** RUN_NOT_ACTIVE 兜底收敛（F12）：stop 打到已终态 run → 前端回到可输入态，不误报「停止失败」 */
  markRunEnded: () => void;

  /** 追加正文/思考增量（每帧至多一次 set，§3.3） */
  appendPart: (partKey: string, text: string) => void;
  /** 整段替换（batch 快速路径 / 快照替换） */
  setPart: (partKey: string, text: string) => void;
  bumpGeneration: () => void;
  reset: () => void;
}

interface ChatData {
  items: string[];
  byId: Map<string, ChatItem>;
  parts: Map<string, ChatPart>;
  activeRun: ActiveRun;
  compactItemId: string | null;
  generation: number;
}

function createChatSlice(): ChatData {
  return {
    items: [],
    byId: new Map(),
    parts: new Map(),
    activeRun: { runId: null, state: 'idle', startedAt: null, error: null },
    compactItemId: null,
    generation: 0,
  };
}

/** 追加条目（append-only：新数组 + 新 Map，§3.3 不可变边界）。 */
function withItem(state: ChatData, item: ChatItem, id = item.id): Partial<ChatData> {
  const byId = new Map(state.byId);
  byId.set(id, item);
  return { items: [...state.items, id], byId };
}

/** 替换既有条目引用（条目不存在时原样返回，缺前者不凭空造态）。 */
function replaceItem(
  state: ChatData,
  id: string,
  patch: (item: ChatItem) => ChatItem,
): Partial<ChatData> {
  const current = state.byId.get(id);
  if (!current) return {};
  const byId = new Map(state.byId);
  byId.set(id, patch(current));
  return { byId };
}

const IDLE_RUN: ActiveRun = { runId: null, state: 'idle', startedAt: null, error: null };

export const useChatStore = create<ChatStore>((set) => ({
  ...createChatSlice(),

  appendUserMessage: ({ id, content, createdAt, runId, agent = null }) =>
    set((s) =>
      // 幂等：重复投递（重放/重连）不重复插条目——items append-only 语义的守门
      s.byId.has(id)
        ? {}
        : withItem(s, {
            kind: 'message',
            id,
            role: 'user',
            createdAt,
            runId,
            agent,
            content,
          }),
    ),

  ensureAssistantMessage: ({ id, createdAt, runId, agent = null }) =>
    set((s) =>
      s.byId.has(id)
        ? {}
        : withItem(s, { kind: 'message', id, role: 'assistant', createdAt, runId, agent }),
    ),

  finishMessage: ({ id, finishReason, structured = null }) =>
    set((s) =>
      replaceItem(s, id, (item) =>
        item.kind === 'message' ? { ...item, finishReason, structured } : item,
      ),
    ),

  appendToolCard: ({ id, tool, args, riskLevel = null, agent = null, createdAt }) =>
    set((s) =>
      s.byId.has(id)
        ? {}
        : withItem(s, {
            kind: 'toolcard',
            id,
            tool,
            args,
            status: 'running',
            riskLevel,
            agent,
            createdAt,
          }),
    ),

  completeToolCall: ({
    id,
    status,
    durationMs,
    error = null,
    exitCode = null,
    resultPreview = null,
    resultRef = null,
    truncated = false,
  }) =>
    set((s) =>
      replaceItem(s, id, (item) =>
        item.kind === 'toolcard'
          ? {
              ...item,
              status,
              durationMs,
              error,
              exitCode,
              resultPreview,
              resultRef,
              truncated,
            }
          : item,
      ),
    ),

  appendApprovalItem: ({ id, tool, status, createdAt }) =>
    set((s) => (s.byId.has(id) ? replaceItem(s, id, (item) => (item.kind === 'approval' ? { ...item, status } : item)) : withItem(s, { kind: 'approval', id, tool, status, createdAt }))),

  setApprovalItemStatus: (id, status) =>
    set((s) =>
      replaceItem(s, id, (item) => (item.kind === 'approval' ? { ...item, status } : item)),
    ),

  appendDiffCard: ({ id, round, fileCount, additionsTotal, deletionsTotal, createdAt }) =>
    set((s) =>
      withItem(s, {
        kind: 'diffcard',
        id,
        round,
        fileCount,
        additionsTotal,
        deletionsTotal,
        verdict: 'pending',
        createdAt,
      }),
    ),

  setDiffCardVerdict: (id, verdict) =>
    set((s) =>
      replaceItem(s, id, (item) => (item.kind === 'diffcard' ? { ...item, verdict } : item)),
    ),

  beginCompact: ({ id, at }) =>
    set((s) => ({
      ...withItem(
        s,
        {
          kind: 'compact_note',
          id,
          phase: 'started',
          beforeTokens: null,
          afterTokens: null,
          updatedAt: at,
        },
        id,
      ),
      compactItemId: id,
    })),

  finishCompact: ({ id, at, beforeTokens, afterTokens }) =>
    set((s) => ({
      ...replaceItem(s, id, (item) =>
        item.kind === 'compact_note'
          ? { ...item, phase: 'finished', beforeTokens, afterTokens, updatedAt: at }
          : item,
      ),
      compactItemId: null,
    })),

  applyRunStatus: ({ subject, phase, runId, createdAt, error = null }) =>
    set(() => {
      if (subject !== 'run') return {}; // subject=compact 由 begin/finishCompact 处理
      switch (phase) {
        case 'started':
          return {
            activeRun: {
              runId,
              state: 'running',
              startedAt: createdAt,
              error,
            } satisfies ActiveRun,
          };
        case 'finished':
        case 'stopped':
        case 'error':
          // finished/stopped/error 同为 run 终态：收敛 idle（error 保留在 activeRun.error
          // 供 UI 提示）。后端 runtime 发完 run_status(error) 即终局、不再补发终态事件——
          // 契约 phase=error 必带 RunErrorObj，runs 行落 error、REST run_state 投影 idle
          return { activeRun: { ...IDLE_RUN, runId, error } satisfies ActiveRun };
        default:
          return {};
      }
    }),

  noteApprovalPending: () =>
    set((s) => ({ activeRun: { ...s.activeRun, state: 'waiting_approval' } })),

  noteApprovalSettled: (allSettled) =>
    set((s) =>
      allSettled && s.activeRun.state === 'waiting_approval'
        ? { activeRun: { ...s.activeRun, state: 'running' } }
        : {},
    ),

  // F12：RUN_NOT_ACTIVE（stop 打到已终态 run）兜底——与 run 终态事件同口径收敛 idle
  // （保留 runId 供参照）；已 idle 幂等不换引用。
  markRunEnded: () =>
    set((s) =>
      s.activeRun.state === 'idle'
        ? {}
        : { activeRun: { ...IDLE_RUN, runId: s.activeRun.runId } satisfies ActiveRun },
    ),

  appendPart: (partKey, text) =>
    set((s) => {
      const current = s.parts.get(partKey);
      const parts = new Map(s.parts);
      parts.set(partKey, { text: (current?.text ?? '') + text, version: (current?.version ?? 0) + 1 });
      return { parts };
    }),

  setPart: (partKey, text) =>
    set((s) => {
      const current = s.parts.get(partKey);
      const parts = new Map(s.parts);
      parts.set(partKey, { text, version: (current?.version ?? 0) + 1 });
      return { parts };
    }),

  bumpGeneration: () => set((s) => ({ generation: s.generation + 1 })),

  reset: () => set(createChatSlice()),
}));
