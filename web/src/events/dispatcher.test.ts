/**
 * dispatcher 管线单测（A4 §5.2；streaming-buffer 规则 6/24；test-plan §5 F1 管线部分）。
 *
 * 覆盖：五步管线（JSON.parse → peekType → 查表 → safeParse → seq 去重 → handler）、
 * A 档负例拒收（缺必填/多字段/类型错/枚举外）、未知类型前向兼容、重复 seq 幂等、
 * **去重水位与 connStore.lastSeq 解耦的回归守卫**、handler 异常隔离、batch 快速路径
 * （delta 不打字机 + generation++）、逐帧 vs batch 共用同一水位。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useChatStore } from '@/features/chat/store';
import { useConnStore } from '@/stores/connStore';

import approvalRequest from '@contracts-fixtures/events/valid/approval_request.json';
import messageCreated from '@contracts-fixtures/events/valid/message_created.json';
import messageDelta from '@contracts-fixtures/events/valid/message_delta.json';
import messageFinished from '@contracts-fixtures/events/valid/message_finished.json';
import runStatus from '@contracts-fixtures/events/valid/run_status.json';

import invalidBadStatusEnum from '@contracts-fixtures/events/invalid/tool_call_finished__bad_status_enum.json';
import invalidExtraField from '@contracts-fixtures/events/invalid/message_delta__extra_field.json';
import invalidMissingDelta from '@contracts-fixtures/events/invalid/message_delta__missing_delta.json';
import invalidSeqString from '@contracts-fixtures/events/invalid/message_delta__seq_string.json';
import unknownEventType from '@contracts-fixtures/events/invalid/unknown_event_type.json';

import { deltaBuffers, flushAllChannels } from './buffers';
import type { EventType } from './contracts';
import {
  applyBatch,
  getDispatcherStats,
  getWatermark,
  handleDataFrame,
  handleFrame,
  parseEventPayload,
  peekType,
  resetDispatcher,
} from './dispatcher';
import { HANDLERS } from './handlers';
import type { SseFrame } from './parser';

const CREATED_SEQ = messageCreated.seq; // 101

function frameOf(payload: unknown, overrides: Partial<SseFrame> = {}): SseFrame {
  return {
    id: String((payload as { seq: number }).seq),
    event: (payload as { type: string }).type,
    data: JSON.stringify(payload),
    ...overrides,
  };
}

/** 造一条与 fixture 同形但 seq/delta 不同的 delta（测试数据，非 wire 类型定义）。 */
function deltaWith(seq: number, delta: string): unknown {
  return { ...messageDelta, seq, delta };
}

beforeEach(() => {
  resetDispatcher();
  deltaBuffers.clear();
  useConnStore.getState().reset();
  useChatStore.getState().reset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('dispatcher：五步管线', () => {
  it('valid fixture 逐帧走通：接受 + 落 store + 水位推进 + connStore 镜像', () => {
    expect(handleDataFrame(JSON.stringify(messageCreated))).toBe('accepted');

    const chat = useChatStore.getState();
    expect(chat.items).toEqual([messageCreated.message_id]);
    expect(chat.byId.get(messageCreated.message_id)).toMatchObject({
      kind: 'message',
      role: 'user',
      content: messageCreated.content,
    });
    expect(getWatermark()).toBe(CREATED_SEQ);
    expect(useConnStore.getState().lastSeq).toBe(CREATED_SEQ);
    expect(getDispatcherStats()).toMatchObject({ accepted: 1, dispatched: 1, frames: 1 });
  });

  it('非法 JSON → invalid + validationErrorCount++ + 不写 store', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);

    expect(handleDataFrame('{"type":')).toBe('invalid');
    expect(useConnStore.getState().validationErrorCount).toBe(1);
    expect(useChatStore.getState().items).toEqual([]);
    expect(getWatermark()).toBeNull();
    expect(errorSpy).toHaveBeenCalled();
  });

  it('缺 type 字段 → invalid（校验失败计数）', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    expect(handleDataFrame(JSON.stringify({ seq: 1, delta: 'x' }))).toBe('invalid');
    expect(useConnStore.getState().validationErrorCount).toBe(1);
    errorSpy.mockRestore();
  });

  it('未知事件类型 → 忽略 + 独立打点（前向兼容，§4.7）', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined);

    expect(handleDataFrame(JSON.stringify(unknownEventType))).toBe('unknown');
    expect(useConnStore.getState().unknownEventTypeCount).toBe(1);
    expect(useConnStore.getState().validationErrorCount).toBe(0);
    expect(useChatStore.getState().items).toEqual([]);
    expect(warnSpy).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['缺必填 delta', invalidMissingDelta],
    ['多余字段（strict）', invalidExtraField],
    ['seq 类型错', invalidSeqString],
    ['枚举外（status）', invalidBadStatusEnum],
  ])('A 档负例被 zod 拒收：%s', (_name, fixture) => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);

    expect(handleDataFrame(JSON.stringify(fixture))).toBe('invalid');
    expect(useConnStore.getState().validationErrorCount).toBe(1);
    expect(useChatStore.getState().items).toEqual([]);
    expect(getWatermark()).toBeNull();
    errorSpy.mockRestore();
  });

  it('peekType：非对象/缺 type 返回 undefined，否则返回字符串 type', () => {
    expect(peekType(null)).toBeUndefined();
    expect(peekType('x')).toBeUndefined();
    expect(peekType({})).toBeUndefined();
    expect(peekType({ type: 42 })).toBeUndefined();
    expect(peekType({ type: 'message_delta' })).toBe('message_delta');
  });

  it('parseEventPayload：直接吃已解析对象也走同一条管线（batch 复用）', () => {
    const parsed = parseEventPayload(messageCreated);
    expect(parsed.ok).toBe(true);
    if (parsed.ok) {
      expect(parsed.type).toBe('message_created');
      expect((parsed.event as { seq: number }).seq).toBe(CREATED_SEQ);
    }
  });

  it('handleFrame 以 data 内的 type 为准（event: 行名不参与路由）', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const frame = frameOf(messageCreated, { event: 'wrong_name' });

    expect(handleFrame(frame)).toBe('accepted');
    expect(useChatStore.getState().items).toEqual([messageCreated.message_id]);
    expect(warnSpy).not.toHaveBeenCalled();
  });
});

describe('dispatcher：seq 去重（唯一锚点 = 模块级水位）', () => {
  it('重复 seq 幂等跳过：重放同 seq 不重复落 store（规则 24 / AC-003-4）', () => {
    expect(handleDataFrame(JSON.stringify(messageCreated))).toBe('accepted');
    expect(handleDataFrame(JSON.stringify(messageCreated))).toBe('duplicate');
    expect(handleDataFrame(JSON.stringify(messageCreated))).toBe('duplicate');

    expect(useChatStore.getState().items).toEqual([messageCreated.message_id]);
    expect(getDispatcherStats()).toMatchObject({ accepted: 1, duplicates: 2 });
  });

  it('更小的 seq 同样跳过，水位不回退', () => {
    expect(handleDataFrame(JSON.stringify(deltaWith(500, 'late')))).toBe('accepted');
    expect(handleDataFrame(JSON.stringify(deltaWith(499, 'stale')))).toBe('duplicate');
    expect(getWatermark()).toBe(500);
  });

  it('回归守卫：connStore.lastSeq 已被传输层推进时，新事件仍被接受（去重不读 connStore）', () => {
    // 传输层（events/sse.ts）会在派发帧前先推进自身水位并镜像到 connStore；
    // 若 dispatcher 误把 connStore.lastSeq 当去重锚点，下面的新事件会被判成重复而丢弃。
    useConnStore.getState().setLastSeq(9999);

    expect(handleDataFrame(JSON.stringify(messageCreated))).toBe('accepted');
    expect(useChatStore.getState().items).toEqual([messageCreated.message_id]);
    expect(useConnStore.getState().lastSeq).toBe(9999); // 单调守卫：镜像不回退
  });

  it('水位空洞不假设密度：跳号 seq 照常接受（事务回滚可有空洞，§4.7）', () => {
    expect(handleDataFrame(JSON.stringify(deltaWith(10, 'a')))).toBe('accepted');
    expect(handleDataFrame(JSON.stringify(deltaWith(77, 'b')))).toBe('accepted');
    expect(getWatermark()).toBe(77);
  });
});

describe('dispatcher：handler 异常隔离', () => {
  it('单个 handler 抛错不中断管线，后续事件照常处理', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const original = HANDLERS.message_created;
    HANDLERS.message_created = () => {
      throw new Error('handler boom');
    };

    try {
      expect(handleDataFrame(JSON.stringify(messageCreated))).toBe('accepted');
      expect(getDispatcherStats().handlerErrors).toBe(1);

      // 后续事件（同会话继续推进 seq）不受影响
      expect(handleDataFrame(JSON.stringify(deltaWith(CREATED_SEQ + 1, 'after boom')))).toBe(
        'accepted',
      );
      flushAllChannels();
      expect(useChatStore.getState().parts.get(`${messageDelta.message_id}:text`)?.text).toBe(
        'after boom',
      );
    } finally {
      HANDLERS.message_created = original;
    }
    errorSpy.mockRestore();
  });
});

describe('dispatcher：applyBatch 重放快速路径（§3.3）', () => {
  it('delta 不打字机：累积为完整文本，一次落 store；事件类照常直达并保序', () => {
    const chatBefore = useChatStore.getState();
    const generationBefore = chatBefore.generation;

    const result = applyBatch([
      messageCreated,
      deltaWith(CREATED_SEQ + 1, '第一批'),
      deltaWith(CREATED_SEQ + 2, '第二批'),
      { ...messageFinished, seq: CREATED_SEQ + 3 },
    ]);

    expect(result).toEqual({ accepted: 2, deltas: 2, duplicates: 0, dropped: 0 });

    const chat = useChatStore.getState();
    // 条目顺序 = 事件发生顺序（user 回声 → assistant → 收口）
    expect(chat.items).toEqual([messageCreated.message_id, messageDelta.message_id]);
    // delta 已整段落 store（无需等帧）
    expect(chat.parts.get(`${messageDelta.message_id}:text`)).toEqual({
      text: '第一批第二批',
      version: 1,
    });
    // 非追加输入：generation 递增，渲染缓存全失效（规则 18）
    expect(chat.generation).toBe(generationBefore + 1);
    expect(chat.byId.get(messageDelta.message_id)).toMatchObject({
      kind: 'message',
      finishReason: 'stop',
    });
    expect(deltaBuffers.channel(`${messageDelta.message_id}:text`, 'text').pendingChars).toBe(0);
  });

  it('batch 逐帧共用同一条水位：批内重复 seq 跳过，批后再来同 seq 仍跳过', () => {
    const result = applyBatch([
      deltaWith(200, 'A'),
      deltaWith(200, 'dup'),
      deltaWith(201, 'B'),
    ]);

    expect(result).toEqual({ accepted: 0, deltas: 2, duplicates: 1, dropped: 0 });
    expect(getWatermark()).toBe(201);
    expect(useChatStore.getState().parts.get(`${messageDelta.message_id}:text`)?.text).toBe('AB');

    // 实时路径接在 batch 之后：水位已推进 → 同 seq 判重
    expect(handleDataFrame(JSON.stringify(deltaWith(201, 'again')))).toBe('duplicate');
  });

  it('batch 内的 A 档负例与未知类型计入 dropped，不写坏 store', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined);

    const result = applyBatch([
      invalidMissingDelta,
      unknownEventType,
      deltaWith(300, 'ok'),
    ]);

    expect(result).toEqual({ accepted: 0, deltas: 1, duplicates: 0, dropped: 2 });
    expect(getWatermark()).toBe(300);
    expect(useConnStore.getState().validationErrorCount).toBe(1);
    expect(useConnStore.getState().unknownEventTypeCount).toBe(1);
    errorSpy.mockRestore();
    warnSpy.mockRestore();
  });

  it('空批处理不 bump generation', () => {
    const before = useChatStore.getState().generation;
    applyBatch([]);
    expect(useChatStore.getState().generation).toBe(before);
  });

  it('batch 里的 text/thinking 两通道分别累积（同一 message_id）', () => {
    applyBatch([
      { ...messageDelta, seq: 400, kind: 'thinking', delta: '思考中' },
      { ...messageDelta, seq: 401, kind: 'text', delta: '正文' },
    ]);

    const key = messageDelta.message_id;
    expect(useChatStore.getState().parts.get(`${key}:thinking`)?.text).toBe('思考中');
    expect(useChatStore.getState().parts.get(`${key}:text`)?.text).toBe('正文');
  });
});

describe('dispatcher：事件类型与 handler 表对齐', () => {
  it('HANDLERS 覆盖契约冻结的全部 12 型事件', () => {
    const types = Object.keys(HANDLERS) as EventType[];
    expect(types).toHaveLength(12);
    expect(types).toContain('browser_action');
  });

  it('approval_request / run_status 等事件类不入缓冲（pendingChars 保持 0）', () => {
    handleDataFrame(JSON.stringify(approvalRequest));
    handleDataFrame(JSON.stringify(runStatus));

    expect(getDispatcherStats().duplicates).toBe(0);
    expect(getDispatcherStats().accepted).toBe(2);
    expect(deltaBuffers.channel('nope:text', 'text').pendingChars).toBe(0);
  });
});
