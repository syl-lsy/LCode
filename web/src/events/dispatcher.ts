/**
 * 事件落地管线（A4 §5.2 逐行；streaming-buffer 规则 6/24）。
 *
 * 固定管线（每帧）：
 *   data(JSON 字符串)
 *     → JSON.parse（失败：丢弃 + console.error + connStore.validationErrorCount++）
 *     → peekType(obj)（读 obj.type，string 校验）
 *     → SCHEMA_BY_TYPE[type] 不存在 → 忽略 + console.warn（前向兼容，§4.7）
 *     → schema.safeParse(obj)
 *         ├─ 失败 → 丢弃 + console.error（含 zod issues）+ validationErrorCount++
 *         └─ 成功 → seq 去重（seq <= 水位 → 跳过；否则水位 = seq）
 *                → handlers[type](event)（delta 类入缓冲，事件类直达 store）
 *
 * **去重水位是模块级变量（本文件私有）**——这是本模块最容易踩的坑：传输层
 * （events/sse.ts）在调用 onFrame **之前**就推进过自己的 lastSeq 了，若把
 * `connStore.lastSeq` 当去重锚点，新事件会被判成「重复」而误丢。connStore.lastSeq
 * 只是 UI 镜像（写入带单调守卫），去重只认这里的 watermark。
 *
 * 校验失败策略（§5.2 取舍已定）：丢弃 + console.error + 计数，**不显示异常块、不 toast**
 * ——契约已冻结 + 服务端出口强校验，校验失败是开发期 bug 信号而非用户可操作错误。
 *
 * handler 异常隔离：单个 handler 抛错只记日志（getDispatcherStats().handlerErrors），
 * 不影响后续事件与连接（sse.ts 侧另有一层 guard）。
 */

import type { z } from 'zod';

import { partKeyOf, useChatStore } from '@/features/chat/store';
import { useConnStore } from '@/stores/connStore';

import { SCHEMA_BY_TYPE, isKnownEventType, type EventType } from './contracts';
import { HANDLERS } from './handlers';
import type { SseFrame } from './parser';

/** 单事件载荷类型（zod 输出推导；禁止手写 wire 形状，ADR-003）。 */
export type EventPayloadOf<K extends EventType> = z.infer<(typeof SCHEMA_BY_TYPE)[K]>;

/** 12 型事件的载荷映射（handlers 表形状来源）。 */
export type EventByType = { [K in EventType]: EventPayloadOf<K> };

/** 任一事件载荷（联合；TS 按 data.type 字面量 narrow）。 */
export type AnyEvent = EventByType[EventType];

/** handler 表：每个 type → 其载荷类型的处理函数（handlers.ts 实现）。 */
export type HandlerMap = { [K in EventType]: (event: EventByType[K]) => void };

export interface DispatcherStats {
  /** 进入管线的帧/payload 总数 */
  frames: number;
  /** 通过校验且未被去重跳过的事件数（含 batch 累积的 delta） */
  accepted: number;
  /** 实际调用 handler 的次数（batch 路径下的 delta 计入缓冲而非 handler，故不计） */
  dispatched: number;
  /** JSON/zod 失败丢弃数 */
  droppedInvalid: number;
  /** seq 去重跳过数（重放/重复帧；不是错误） */
  duplicates: number;
  /** 未知事件类型忽略数（前向兼容） */
  unknownTypes: number;
  /** handler 抛错次数（已隔离） */
  handlerErrors: number;
}

export type FrameOutcome = 'accepted' | 'duplicate' | 'invalid' | 'unknown';

interface ParsedOk {
  readonly ok: true;
  readonly type: EventType;
  readonly event: AnyEvent;
}

interface ParsedFail {
  readonly ok: false;
  readonly reason: 'parse' | 'invalid' | 'unknown';
}

type ParseOutcome = ParsedOk | ParsedFail;

// ---------------------------------------------------------------- 模块级状态

/** 去重水位：唯一去重锚点（会话内 seq 单调，重放保序同 seq）。 */
let watermark: number | null = null;

function createStats(): DispatcherStats {
  return {
    frames: 0,
    accepted: 0,
    dispatched: 0,
    droppedInvalid: 0,
    duplicates: 0,
    unknownTypes: 0,
    handlerErrors: 0,
  };
}

let stats: DispatcherStats = createStats();

/** 已消费水位（= 最后一个被接受事件的 seq）；仅供对账显示，不用于去重。 */
export function getWatermark(): number | null {
  return watermark;
}

export function getDispatcherStats(): Readonly<DispatcherStats> {
  return { ...stats };
}

/** 测试隔离：重置水位与计数（生产不调用——水位必须跨重连保持）。 */
export function resetDispatcher(): void {
  watermark = null;
  stats = createStats();
}

// ---------------------------------------------------------------- 解析与派发

/** 读信封内的 type（契约：type 冗余在 data 内，§5.1）。 */
export function peekType(value: unknown): string | undefined {
  if (typeof value !== 'object' || value === null) return undefined;
  const type = (value as { type?: unknown }).type;
  return typeof type === 'string' ? type : undefined;
}

/** JSON.parse → peekType → 查表 → safeParse（§5.2 前三步）。 */
export function parseEventPayload(payload: string | unknown): ParseOutcome {
  let value: unknown = payload;
  if (typeof payload === 'string') {
    try {
      value = JSON.parse(payload);
    } catch (error) {
      stats.droppedInvalid += 1;
      useConnStore.getState().bumpValidationErrors();
      console.error('[dispatcher] 帧 data 不是合法 JSON（丢弃）：', error);
      return { ok: false, reason: 'parse' };
    }
  }

  const type = peekType(value);
  if (type === undefined) {
    stats.droppedInvalid += 1;
    useConnStore.getState().bumpValidationErrors();
    console.error('[dispatcher] 事件缺少 type 字段（丢弃）：', value);
    return { ok: false, reason: 'invalid' };
  }

  if (!isKnownEventType(type)) {
    // 前向兼容：服务端新增事件类型时老前端忽略 + 记日志（§4.7）
    stats.unknownTypes += 1;
    useConnStore.getState().bumpUnknownEventTypes();
    console.warn(`[dispatcher] 未知事件类型 "${type}"（忽略 + 打点；browser_action 属 P1 占位）`);
    return { ok: false, reason: 'unknown' };
  }

  const result = SCHEMA_BY_TYPE[type].safeParse(value);
  if (!result.success) {
    stats.droppedInvalid += 1;
    useConnStore.getState().bumpValidationErrors();
    console.error(`[dispatcher] 事件 zod 校验失败（丢弃）type=${type}：`, result.error.issues);
    return { ok: false, reason: 'invalid' };
  }
  return { ok: true, type, event: result.data as AnyEvent };
}

/**
 * seq 去重（唯一锚点 = 模块级 watermark）+ 水位镜像。
 * 返回 true = 该事件是新事件（调用方负责派发）。
 */
function commit(parsed: ParsedOk): boolean {
  const seq = (parsed.event as { seq: number }).seq;
  if (watermark !== null && seq <= watermark) {
    stats.duplicates += 1;
    return false;
  }
  watermark = seq;
  // 镜像到 connStore 仅供 UI 显示（单调守卫）；去重永不读它
  useConnStore.getState().setLastSeq(seq);
  stats.accepted += 1;
  return true;
}

/** handler 调用壳：异常隔离的唯一实现点（单个 handler 抛错不影响后续事件）。 */
export function dispatchToHandler(type: EventType, event: AnyEvent): void {
  const handler = HANDLERS[type] as (payload: AnyEvent) => void;
  stats.dispatched += 1;
  try {
    handler(event);
  } catch (error) {
    stats.handlerErrors += 1;
    console.error(`[dispatcher] handler(${type}) 异常（已隔离，不影响后续事件）:`, error);
  }
}

/** 处理一帧 data 字符串（完整管线；sse.ts 的 onFrame 直接接这里）。 */
export function handleDataFrame(data: string): FrameOutcome {
  stats.frames += 1;
  const parsed = parseEventPayload(data);
  if (!parsed.ok) return parsed.reason === 'unknown' ? 'unknown' : 'invalid';
  if (!commit(parsed)) return 'duplicate';
  dispatchToHandler(parsed.type, parsed.event);
  return 'accepted';
}

/**
 * 处理一个 SSE 帧：以 **data 内的 type** 为准（§5.1：type 在 data 内冗余），
 * `event:` 行名不参与路由（契约保证两者一致）。
 */
export function handleFrame(frame: SseFrame): FrameOutcome {
  return handleDataFrame(frame.data);
}

export function handleFrames(frames: readonly SseFrame[]): FrameOutcome[] {
  return frames.map((frame) => handleFrame(frame));
}

// ---------------------------------------------------------------- batch 快速路径

export interface BatchResult {
  /** 事件类（直达 store）接受数 */
  accepted: number;
  /** delta 类累积数（合并后一次性落 store） */
  deltas: number;
  /** seq 去重跳过数 */
  duplicates: number;
  /** 校验失败/未知类型丢弃数 */
  dropped: number;
}

interface DeltaAccumulator {
  readonly messageId: string;
  readonly runId: string | null;
  readonly agent: EventByType['message_delta']['agent'];
  readonly createdAt: string;
  readonly text: string;
}

/**
 * 重放/首连全量批处理（§3.3 batch 快速路径）：
 * - delta **不打字机**：按 partKey 累积为完整文本，批末一次性落 store；
 * - 事件类照常直达 store（重放顺序 = 发生顺序，AC-003-4）；
 * - seq 去重与实时路径共用同一条水位（重放保序同 seq，规则 24）；
 * - 批末 `generation++`：非追加输入使渲染缓存整体失效（streaming-buffer 规则 18）。
 */
export function applyBatch(payloads: readonly (string | unknown)[]): BatchResult {
  const result: BatchResult = { accepted: 0, deltas: 0, duplicates: 0, dropped: 0 };
  const pending = new Map<string, DeltaAccumulator>();
  /** 已建条目的 assistant 消息（每条只 ensure 一次，后续 delta 是 no-op） */
  const ensured = new Set<string>();

  for (const payload of payloads) {
    stats.frames += 1;
    const parsed = parseEventPayload(payload);
    if (!parsed.ok) {
      result.dropped += 1;
      continue;
    }
    if (!commit(parsed)) {
      result.duplicates += 1;
      continue;
    }
    if (parsed.type === 'message_delta') {
      const event = parsed.event as EventByType['message_delta'];
      const kind = event.kind;
      const key = partKeyOf(event.message_id, kind);
      // 条目**先于**后续事件类建好：message_finished 的收口 patch 才有落点（顺序 = 发生顺序）
      if (!ensured.has(event.message_id)) {
        useChatStore.getState().ensureAssistantMessage({
          id: event.message_id,
          createdAt: event.created_at,
          runId: event.run_id ?? null,
          agent: event.agent ?? null,
        });
        ensured.add(event.message_id);
      }
      const existing = pending.get(key);
      pending.set(key, {
        messageId: event.message_id,
        runId: event.run_id ?? null,
        agent: event.agent ?? null,
        createdAt: existing?.createdAt ?? event.created_at,
        text: (existing?.text ?? '') + event.delta,
      });
      result.deltas += 1;
      continue;
    }
    dispatchToHandler(parsed.type, parsed.event);
    result.accepted += 1;
  }

  // 一次性落 store：每个 partKey 一次 setPart（delta 文本整段，不打字机）
  for (const [key, accumulator] of pending) {
    const current = useChatStore.getState().parts.get(key)?.text ?? '';
    useChatStore.getState().setPart(key, current + accumulator.text);
  }

  if (result.accepted > 0 || result.deltas > 0) useChatStore.getState().bumpGeneration();
  return result;
}
