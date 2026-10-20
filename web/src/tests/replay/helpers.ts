/**
 * 契约 fixtures 回放工具（A4 §10.2 / test-plan §3.3/§3.4）：
 *
 * - fixtures 经 `@contracts-fixtures` 别名直引 contracts/tests/fixtures/events/valid/*.json
 *   （不复制文件——契约变更即前端测试红，同源原则）；
 * - feedFixtures：逐条 handleDataFrame（与真实帧派发同一管线，含 seq 去重）；
 * - feedRaw：自定义事件对象（fixtures 未覆盖的字段组合用构造事件补齐）。
 *
 * 事件对象必须满足生成 zod schema（safeParse 是第一道闸）；type 字段冗余照契约。
 */

import { act } from '@testing-library/react';

import { flushAllChannels, terminalBuffer } from '@/events/buffers';
import { handleDataFrame } from '@/events/dispatcher';

export type FixtureEvent = Record<string, unknown>;

/**
 * 逐条派发（JSON 字符串 → 完整管线，含 seq 去重）+ 终态冲刷（delta 通道与终端
 * 100ms 限频缓冲一次落 store，等价「重放完成」）——返回每条的 FrameOutcome 便于断言。
 * act 包裹：store 写入在 React 测试环境中必须经 act，否则组件不重渲。
 */
export function feedFixtures(events: readonly FixtureEvent[]): string[] {
  let outcomes: string[] = [];
  act(() => {
    outcomes = events.map((event) => handleDataFrame(JSON.stringify(event)));
    flushAllChannels();
    terminalBuffer.flush();
  });
  return outcomes;
}

/** 派发单条自定义事件（同 feedFixtures 的冲刷语义）。 */
export function feedRaw(event: FixtureEvent): string {
  let outcome = '';
  act(() => {
    outcome = handleDataFrame(JSON.stringify(event));
    flushAllChannels();
    terminalBuffer.flush();
  });
  return outcome;
}

/** 构造 message_delta（kind/text 默认 text；seq 显式传，保序由调用方负责）。 */
export function delta(input: {
  seq: number;
  messageId: string;
  delta: string;
  kind?: 'text' | 'thinking';
  sessionId?: string;
  createdAt?: string;
}): FixtureEvent {
  return {
    seq: input.seq,
    session_id: input.sessionId ?? '0192f0a1-1111-4a1a-9c2d-000000000001',
    run_id: '0192f0a1-2222-4a1a-9c2d-000000000002',
    created_at: input.createdAt ?? '2026-10-17T09:30:05.654321+00:00',
    type: 'message_delta',
    message_id: input.messageId,
    role: 'assistant',
    kind: input.kind ?? 'text',
    delta: input.delta,
  };
}

/** 基础事件骨架（session/时间固定，字段覆盖合并）。 */
export function eventBase(seq: number, type: string, extra: FixtureEvent = {}): FixtureEvent {
  return {
    seq,
    session_id: '0192f0a1-1111-4a1a-9c2d-000000000001',
    run_id: '0192f0a1-2222-4a1a-9c2d-000000000002',
    created_at: '2026-10-17T09:30:00.000000+00:00',
    type,
    ...extra,
  };
}
