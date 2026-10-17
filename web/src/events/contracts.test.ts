/**
 * 契约生成物接入冒烟测试（M1 阶段 2 核心验证点）。
 *
 * 双侧接通证明：
 * - TS 侧：`@contracts` 类型可导入，且 zod schema 的 parse 输出可赋值给生成的
 *   interface（本文件被 `tsc --noEmit` 覆盖即证明类型接通）；
 * - 运行时侧：SCHEMA_BY_TYPE 对全部 valid fixtures 接受、对 zod 可拒的 invalid
 *   fixtures 拒绝、对未知 type 不进查表（前向兼容路径）。
 *
 * fixtures 经 `@contracts-fixtures` 别名直引 contracts/tests/fixtures/（不复制，
 * A4 §10.2——契约与前端测试同源，契约变更即测试红）。
 * 完整正/负例矩阵（含 11 条事件跨字段豁免）由契约自己的守门负责：
 * contracts/tools/ts/validate_fixtures.mjs；本测试只覆盖前端接入面。
 */
import { describe, expect, it } from 'vitest';
import type { z } from 'zod';

import type { MessageDeltaEvent } from '@contracts/events';
import { MessageDeltaEventSchema } from '@contracts/events';

import approvalRequest from '@contracts-fixtures/events/valid/approval_request.json';
import approvalResolved from '@contracts-fixtures/events/valid/approval_resolved.json';
import browserAction from '@contracts-fixtures/events/valid/browser_action.json';
import diffReady from '@contracts-fixtures/events/valid/diff_ready.json';
import diffResolved from '@contracts-fixtures/events/valid/diff_resolved.json';
import messageCreated from '@contracts-fixtures/events/valid/message_created.json';
import messageDelta from '@contracts-fixtures/events/valid/message_delta.json';
import messageFinished from '@contracts-fixtures/events/valid/message_finished.json';
import runStatus from '@contracts-fixtures/events/valid/run_status.json';
import runStatusCompact from '@contracts-fixtures/events/valid/run_status_compact.json';
import sandboxOutput from '@contracts-fixtures/events/valid/sandbox_output.json';
import sandboxOutputLifecycle from '@contracts-fixtures/events/valid/sandbox_output_lifecycle.json';
import toolCallFinished from '@contracts-fixtures/events/valid/tool_call_finished.json';
import toolCallFinishedError from '@contracts-fixtures/events/valid/tool_call_finished_error.json';
import toolCallStarted from '@contracts-fixtures/events/valid/tool_call_started.json';

import invalidBadStatusEnum from '@contracts-fixtures/events/invalid/tool_call_finished__bad_status_enum.json';
import invalidExtraField from '@contracts-fixtures/events/invalid/message_delta__extra_field.json';
import invalidMissingDelta from '@contracts-fixtures/events/invalid/message_delta__missing_delta.json';
import invalidSeqString from '@contracts-fixtures/events/invalid/message_delta__seq_string.json';
import unknownEventType from '@contracts-fixtures/events/invalid/unknown_event_type.json';

import { SCHEMA_BY_TYPE, isKnownEventType } from './contracts';

const VALID_FIXTURES: ReadonlyArray<readonly [name: string, fixture: unknown]> = [
  ['message_created', messageCreated],
  ['message_delta', messageDelta],
  ['message_finished', messageFinished],
  ['tool_call_started', toolCallStarted],
  ['tool_call_finished', toolCallFinished],
  ['tool_call_finished_error', toolCallFinishedError],
  ['approval_request', approvalRequest],
  ['approval_resolved', approvalResolved],
  ['diff_ready', diffReady],
  ['diff_resolved', diffResolved],
  ['sandbox_output', sandboxOutput],
  ['sandbox_output_lifecycle', sandboxOutputLifecycle],
  ['run_status', runStatus],
  ['run_status_compact', runStatusCompact],
  ['browser_action', browserAction],
];

// 选取"zod 必拒"的负例（缺必填/多字段/类型错/枚举外）。
// 跨字段 model_validator 类负例（totals 求和、channel×phase 等）不在此列——
// 它们属契约侧豁免清单（INVALID_EXEMPTIONS），前端 zod 依设计不兜底。
const INVALID_FIXTURES: ReadonlyArray<readonly [name: string, fixture: unknown]> = [
  ['message_delta__missing_delta', invalidMissingDelta],
  ['message_delta__extra_field', invalidExtraField],
  ['message_delta__seq_string', invalidSeqString],
  ['tool_call_finished__bad_status_enum', invalidBadStatusEnum],
];

/** 读取 fixture 信封的 type 字段（契约：type 冗余在 data 内，onmessage 直读）。 */
function peekType(fixture: unknown): string {
  expect(fixture).toBeTypeOf('object');
  const type = (fixture as { type?: unknown }).type;
  expect(type).toBeTypeOf('string');
  return type as string;
}

function schemaFor(fixture: unknown): z.ZodTypeAny {
  const type = peekType(fixture);
  if (!isKnownEventType(type)) {
    throw new Error(`fixture type 不在查表内：${type}`);
  }
  return SCHEMA_BY_TYPE[type];
}

describe('契约生成物接入（@contracts 类型 + zod 双侧冒烟）', () => {
  it('TS 类型接通：schema.parse 输出可赋值给生成的 MessageDeltaEvent 接口', () => {
    // 本断言的真正门禁是 typecheck（tsc --noEmit）：赋值不成立则编译失败。
    const parsed: MessageDeltaEvent = MessageDeltaEventSchema.parse(messageDelta);
    expect(parsed.type).toBe('message_delta');
    expect(parsed.seq).toBe(118);
    expect(parsed.kind).toBe('text');
  });

  it.each(VALID_FIXTURES)('运行时接通：valid fixture 被查表 schema 接受（%s）', (_name, fixture) => {
    const result = schemaFor(fixture).safeParse(fixture);
    if (!result.success) {
      throw new Error(`valid fixture 被拒：${JSON.stringify(result.error.issues)}`);
    }
    expect(result.success).toBe(true);
  });

  it.each(INVALID_FIXTURES)('运行时防御：invalid fixture 被查表 schema 拒绝（%s）', (_name, fixture) => {
    expect(schemaFor(fixture).safeParse(fixture).success).toBe(false);
  });

  it('未知事件类型不进查表（前向兼容忽略路径，A4 §4.7 / AC-008-2）', () => {
    const type = peekType(unknownEventType);
    expect(isKnownEventType(type)).toBe(false);
  });

  it('查表覆盖契约冻结的全部 12 型事件', () => {
    expect(Object.keys(SCHEMA_BY_TYPE)).toHaveLength(12);
  });
});
