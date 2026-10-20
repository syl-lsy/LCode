/**
 * 通道缓冲（A4 §6.1；streaming-buffer 规则 10-13）。
 *
 * 三条通道分立（规则 12）：
 * - 正文 text：ChannelBuffer，floor=2 / k=0.12——打字感 + 追赶；
 * - 思考 thinking：ChannelBuffer，k=0.25（追赶优先，用户扫读，直出更快）；
 * - 终端 stdout/stderr：TerminalBuffer——**不做打字机**，行缓冲 + 100ms 限频 append
 *   + 环形上限（超出滚存）。
 * 事件类（message_created / tool_call_* / approval_* / diff_* / run_status）直达 store，
 * 不入队（§6.1）。
 *
 * 有界性（§4.6/规则 13）：每通道 64KB 待渲染上限，超限直接整段 flush 降级为整段渲染
 * ——页面隐藏/断网时唯一可能无界增长的路径。
 *
 * 终态冲刷（规则 10）：message_finished / run 终态 / 用户 abort → flushAll()；
 * 已收内容不丢。
 */

import { useChatStore, type PartKind } from '@/features/chat/store';
import { useTerminalStore } from '@/features/workbench/store';

import { framePump, type FramePump } from './scheduler';

/** 通道定速参数（§6.1 参数表；floor=每帧起步字符数，catchUp=追赶比例） */
export const CHANNEL_PARAMS: Record<PartKind, { readonly floor: number; readonly catchUp: number }> = {
  text: { floor: 2, catchUp: 0.12 },
  thinking: { floor: 2, catchUp: 0.25 },
};

/** 每通道待渲染字节上限（64KB，§4.6） */
export const MAX_PENDING_BYTES = 64 * 1024;

/** 终端 100ms 限频窗口（§6.1/§6.3） */
export const TERMINAL_FLUSH_MS = 100;

/** 终端待处理行上限：超限立即冲刷，防单次大批量输出撑爆内存 */
export const TERMINAL_MAX_PENDING_LINES = 500;

/** UTF-8 字节长度（自建而非 TextEncoder 分配，按 push 频率计成本可控）。 */
export function utf8ByteLength(text: string): number {
  let bytes = 0;
  for (let i = 0; i < text.length; i += 1) {
    const code = text.charCodeAt(i);
    if (code < 0x80) bytes += 1;
    else if (code < 0x800) bytes += 2;
    else if (code >= 0xd800 && code <= 0xdbff && i + 1 < text.length) {
      const next = text.charCodeAt(i + 1);
      if (next >= 0xdc00 && next <= 0xdfff) {
        bytes += 4;
        i += 1;
      } else {
        bytes += 3;
      }
    } else bytes += 3;
  }
  return bytes;
}

export interface ChannelBufferOptions {
  readonly partKey: string;
  readonly kind: PartKind;
  readonly pump: FramePump;
  /** 追加 sink（默认写 chatStore.appendPart；注入用于单测） */
  readonly sink?: (partKey: string, text: string) => void;
  readonly floor?: number;
  readonly catchUp?: number;
  readonly maxPendingBytes?: number;
}

/** 单通道打字机缓冲：push 入队 → 每帧 drain `max(floor, backlog × k)` 字符。 */
export class ChannelBuffer {
  readonly partKey: string;
  readonly kind: PartKind;
  private readonly pump: FramePump;
  private readonly sink: (partKey: string, text: string) => void;
  private readonly floor: number;
  private readonly catchUp: number;
  private readonly maxPendingBytes: number;
  private pending = '';
  private bytes = 0;

  constructor(options: ChannelBufferOptions) {
    const params = CHANNEL_PARAMS[options.kind];
    this.partKey = options.partKey;
    this.kind = options.kind;
    this.pump = options.pump;
    this.sink =
      options.sink ?? ((partKey, text) => useChatStore.getState().appendPart(partKey, text));
    this.floor = options.floor ?? params.floor;
    this.catchUp = options.catchUp ?? params.catchUp;
    this.maxPendingBytes = options.maxPendingBytes ?? MAX_PENDING_BYTES;
  }

  get pendingChars(): number {
    return this.pending.length;
  }

  get pendingBytes(): number {
    return this.bytes;
  }

  /** 入队一个 delta；超上限立即整段冲刷降级（§4.6）。 */
  push(text: string): void {
    if (text === '') return;
    this.pending += text;
    this.bytes += utf8ByteLength(text);
    if (this.bytes > this.maxPendingBytes) {
      this.flushAll();
      return;
    }
    this.pump.request();
  }

  /**
   * batch/重放快速路径：不打字机，整段落 store（§3.3）。
   * 已排队内容与新文本合并为一次 sink 调用，保证顺序与「一次性落 store」。
   */
  appendImmediate(text: string): void {
    const merged = this.pending + text;
    if (merged === '') return;
    this.pending = '';
    this.bytes = 0;
    this.sink(this.partKey, merged);
  }

  /** 每帧 drain 一次：n = max(floor, ceil(backlog × k))；仍有 backlog 时预约下一帧。 */
  drainFrame(): void {
    if (this.pending === '') return;
    const chunkLength = Math.max(this.floor, Math.ceil(this.pending.length * this.catchUp));
    const chunk = this.pending.slice(0, chunkLength);
    this.pending = this.pending.slice(chunkLength);
    this.bytes = utf8ByteLength(this.pending);
    this.sink(this.partKey, chunk);
    if (this.pending !== '') this.pump.request(); // 泵是请求驱动：残余 backlog 续帧，直至清空
  }

  /** 终态冲刷：剩余内容一次交出（规则 10，已收内容不丢）。 */
  flushAll(): void {
    if (this.pending === '') return;
    const rest = this.pending;
    this.pending = '';
    this.bytes = 0;
    this.sink(this.partKey, rest);
  }
}

/** delta 通道注册表：一次注册 drain 回调，按 partKey 取/建通道。 */
export class DeltaBufferRegistry {
  private readonly pump: FramePump;
  private readonly channels = new Map<string, ChannelBuffer>();

  constructor(pump: FramePump = framePump) {
    this.pump = pump;
    this.pump.add(() => this.drainAll());
  }

  channel(partKey: string, kind: PartKind): ChannelBuffer {
    const existing = this.channels.get(partKey);
    if (existing) return existing;
    const created = new ChannelBuffer({ partKey, kind, pump: this.pump });
    this.channels.set(partKey, created);
    return created;
  }

  push(partKey: string, kind: PartKind, text: string): void {
    this.channel(partKey, kind).push(text);
  }

  appendImmediate(partKey: string, kind: PartKind, text: string): void {
    this.channel(partKey, kind).appendImmediate(text);
  }

  flushChannel(partKey: string): void {
    this.channels.get(partKey)?.flushAll();
  }

  /** 全通道冲刷（终态：message_finished / run 终态 / 用户停止 / 页面卸载，规则 10）。 */
  flushAll(): void {
    for (const channel of this.channels.values()) channel.flushAll();
  }

  drainAll(): void {
    for (const channel of this.channels.values()) channel.drainFrame();
  }

  get size(): number {
    return this.channels.size;
  }

  /** 清空通道（测试隔离用；生产不做通道回收——会话内 partKey 数量有界） */
  clear(): void {
    this.channels.clear();
  }
}

/** App 级 delta 缓冲注册表（handlers.ts 的 delta 落点）。 */
export const deltaBuffers = new DeltaBufferRegistry();

/** 单通道终态冲刷的便捷入口（message_finished）。 */
export function flushChannel(partKey: string): void {
  deltaBuffers.flushChannel(partKey);
}

/** 全通道终态冲刷（run 终态 / 用户停止 / abort）。 */
export function flushAllChannels(): void {
  deltaBuffers.flushAll();
}

// ---------------------------------------------------------------- 终端通道

type TerminalSink = (execId: string, lines: readonly string[]) => void;

export interface TerminalBufferOptions {
  readonly sink?: TerminalSink;
  readonly throttleMs?: number;
  readonly maxPendingLines?: number;
  readonly now?: () => number;
  readonly setTimer?: (cb: () => void, ms: number) => number;
  readonly clearTimer?: (handle: number) => void;
}

interface ExecPending {
  lines: string[];
  /** 未换行的尾段：留在缓冲内，待换行到达或显式 flush 时落库（避免半行被拆成多行） */
  partial: string;
}

/**
 * 终端回显缓冲（§6.1/§6.3）：无打字机，行缓冲 + 100ms 限频 append。
 * 环形上限由 store 侧保证（MAX_TERMINAL_LINES，超出滚存计数）。
 * 未换行的尾段：限频落库只交完整行（半行不拆成两行）；显式 flush（终态）全量交出。
 */
export class TerminalBuffer {
  private readonly sink: TerminalSink;
  private readonly throttleMs: number;
  private readonly maxPendingLines: number;
  private readonly setTimer: (cb: () => void, ms: number) => number;
  private readonly clearTimer: (handle: number) => void;
  private readonly pending = new Map<string, ExecPending>();
  private timer: number | null = null;

  constructor(options: TerminalBufferOptions = {}) {
    this.sink =
      options.sink ?? ((execId, lines) => useTerminalStore.getState().appendLines(execId, lines));
    this.throttleMs = options.throttleMs ?? TERMINAL_FLUSH_MS;
    this.maxPendingLines = options.maxPendingLines ?? TERMINAL_MAX_PENDING_LINES;
    this.setTimer =
      options.setTimer ?? ((cb, ms) => setTimeout(cb, ms) as unknown as number);
    this.clearTimer = options.clearTimer ?? ((handle) => clearTimeout(handle));
  }

  get pendingLines(): number {
    let total = 0;
    for (const entry of this.pending.values()) total += entry.lines.length;
    return total;
  }

  /** 追加一段 stdout/stderr 文本（行缓冲）。 */
  append(execId: string, data: string): void {
    if (data === '') return;
    let entry = this.pending.get(execId);
    if (!entry) {
      entry = { lines: [], partial: '' };
      this.pending.set(execId, entry);
    }
    const segments = (entry.partial + data).split('\n');
    entry.partial = segments.pop() ?? '';
    for (const line of segments) entry.lines.push(line);
    if (this.pendingLines >= this.maxPendingLines) {
      this.flush();
      return;
    }
    this.scheduleFlush();
  }

  /** 立即落库（run 终态 / 待处理超限）：完整行 + 尾段（若存在）作为最后一行交出。 */
  flush(): void {
    if (this.timer !== null) {
      this.clearTimer(this.timer);
      this.timer = null;
    }
    this.emit(true);
  }

  /** 丢弃全部待处理内容（测试隔离用）。 */
  clear(): void {
    if (this.timer !== null) {
      this.clearTimer(this.timer);
      this.timer = null;
    }
    this.pending.clear();
  }

  /**
   * 交出待处理行。`includePartial`：
   * - 限频定时器路径（false）→ 只交完整行，未换行尾段留在缓冲内，避免半行被拆成两行；
   * - 显式 flush（true，终态）→ 尾段一并交出，已收内容不丢（规则 10）。
   */
  private emit(includePartial: boolean): void {
    for (const [execId, entry] of this.pending) {
      const partial = includePartial ? entry.partial : '';
      const lines = partial === '' ? entry.lines : [...entry.lines, partial];
      entry.lines = [];
      entry.partial = includePartial ? '' : entry.partial;
      if (lines.length > 0) this.sink(execId, lines);
      if (entry.lines.length === 0 && entry.partial === '') this.pending.delete(execId);
    }
  }

  private scheduleFlush(): void {
    if (this.timer !== null) return;
    this.timer = this.setTimer(() => {
      this.timer = null;
      this.emit(false);
    }, this.throttleMs);
  }
}

/** App 级终端缓冲（handlers.ts 的 sandbox_output(stdout/stderr) 落点）。 */
export const terminalBuffer = new TerminalBuffer();
