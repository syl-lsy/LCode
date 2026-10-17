import {
  ApprovalRequestEventSchema,
  ApprovalResolvedEventSchema,
  BrowserActionEventSchema,
  DiffReadyEventSchema,
  DiffResolvedEventSchema,
  MessageCreatedEventSchema,
  MessageDeltaEventSchema,
  MessageFinishedEventSchema,
  RunStatusEventSchema,
  SandboxOutputEventSchema,
  ToolCallFinishedEventSchema,
  ToolCallStartedEventSchema,
} from '@contracts/events';

/**
 * 契约生成物接入点（A4 §5.1）——全站唯一允许"知道全部事件类型"的文件。
 *
 * wire 类型与 zod schema 一律来自 contracts/generated/ts（ADR-003 契约单源），
 * 业务代码禁止手写 wire 形状。查表优于 union 试错：
 * ① 未知 type 的前向兼容分支天然存在（查表 miss → 忽略 + console.warn，A4 §4.7）；
 * ② 单 schema safeParse，不逐分支试错；
 * ③ `keyof typeof SCHEMA_BY_TYPE` 即事件类型全集，编译期防拼写错。
 *
 * 注意（ADR-003 已知限制）：TS 侧校验弱于 Python 侧（model_validator 跨字段规则
 * 生成不过去）——前端 zod 只做边界防御，服务端 TypeAdapter(Event) 出口是唯一闸门。
 */
export const SCHEMA_BY_TYPE = {
  message_created: MessageCreatedEventSchema,
  message_delta: MessageDeltaEventSchema,
  message_finished: MessageFinishedEventSchema,
  tool_call_started: ToolCallStartedEventSchema,
  tool_call_finished: ToolCallFinishedEventSchema,
  approval_request: ApprovalRequestEventSchema,
  approval_resolved: ApprovalResolvedEventSchema,
  diff_ready: DiffReadyEventSchema,
  diff_resolved: DiffResolvedEventSchema,
  sandbox_output: SandboxOutputEventSchema,
  run_status: RunStatusEventSchema,
  browser_action: BrowserActionEventSchema, // P1 占位：P0 服务端不得产出（A4 §5.3）
} as const;

/** 事件类型全集（12 型，契约 M1 冻结 v1）。 */
export type EventType = keyof typeof SCHEMA_BY_TYPE;

/**
 * dispatcher 管线第 2/3 步的基件（A4 §5.2）：type 探测 + 查表。
 * miss → 调用方走"忽略 + console.warn + connStore 打点"路径（M2 任务 4 实现）。
 */
export function isKnownEventType(type: string): type is EventType {
  return Object.hasOwn(SCHEMA_BY_TYPE, type);
}
