/**
 * handlers 映射表单测（A4 §5.3 逐行；test-plan §2 的 F1 管线部分）。
 *
 * 手法：用契约 valid fixtures 经 zod 解析得到**类型正确的事件载荷**，直接调用
 * HANDLERS[type]，断言目标 store 的写入结果（delta 类断言入缓冲，冲后落 store）。
 * 这里不重复 dispatcher 的校验/去重职责（见 dispatcher.test.ts）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useApprovalStore } from '@/features/approval/store';
import { partKeyOf, useChatStore } from '@/features/chat/store';
import { useCostStore } from '@/features/costs/store';
import { useSessionStore } from '@/features/sessions/store';
import { useTodoStore } from '@/features/todo/store';
import { useDiffStore, useTerminalStore } from '@/features/workbench/store';

import { ApprovalRequestEventSchema, ApprovalResolvedEventSchema } from '@contracts/events';
import { DiffReadyEventSchema, DiffResolvedEventSchema } from '@contracts/events';
import { MessageCreatedEventSchema, MessageFinishedEventSchema } from '@contracts/events';
import { MessageDeltaEventSchema, RunStatusEventSchema } from '@contracts/events';
import { SandboxOutputEventSchema, ToolCallFinishedEventSchema } from '@contracts/events';
import { ToolCallStartedEventSchema } from '@contracts/events';
import type { SessionDetail } from '@contracts/rest';

import approvalRequest from '@contracts-fixtures/events/valid/approval_request.json';
import approvalResolved from '@contracts-fixtures/events/valid/approval_resolved.json';
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

import { deltaBuffers, flushAllChannels, terminalBuffer } from './buffers';
import { HANDLERS, resetHandlerWarnings } from './handlers';

const SESSION_ID = messageCreated.session_id;
const MESSAGE_ID = messageDelta.message_id;

/** 造一条已加载的会话（REST 首载语义；事件侧只更新已存在条目）。 */
function seedSession(overrides: Partial<SessionDetail> = {}): SessionDetail {
  const session: SessionDetail = {
    id: SESSION_ID,
    title: '会话',
    status: 'active',
    model: 'qwen3-max',
    reasoning_effort: 'medium',
    approval_mode: 'manual',
    sandbox_provider: 'bailian',
    run_state: 'idle',
    created_at: '2026-10-17T09:00:00+00:00',
    updated_at: '2026-10-17T09:00:00+00:00',
    last_active_at: '2026-10-17T09:00:00+00:00',
    ...overrides,
  };
  useSessionStore.getState().setList([session]);
  return session;
}

beforeEach(() => {
  deltaBuffers.clear();
  terminalBuffer.clear();
  resetHandlerWarnings();
  useChatStore.getState().reset();
  useSessionStore.getState().reset();
  useApprovalStore.getState().reset();
  useDiffStore.getState().reset();
  useTerminalStore.getState().reset();
  useTodoStore.getState().reset();
  useCostStore.getState().reset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('handlers：消息族', () => {
  it('message_created → chat 追加 user 消息 + 会话活跃度/运行徽标', () => {
    seedSession();
    const event = MessageCreatedEventSchema.parse(messageCreated);

    HANDLERS.message_created(event);

    const item = useChatStore.getState().byId.get(event.message_id);
    expect(item).toMatchObject({ kind: 'message', role: 'user', content: event.content });
    const session = useSessionStore.getState().list[0];
    expect(session?.last_active_at).toBe(event.created_at);
    expect(session?.run_state).toBe('running');
  });

  it('message_delta → 入缓冲（不直接写 store）；冲后落 parts（kind 分通道）', () => {
    const event = MessageDeltaEventSchema.parse(messageDelta);

    HANDLERS.message_delta(event);

    // assistant 条目由首个 delta 建立
    expect(useChatStore.getState().byId.get(event.message_id)).toMatchObject({
      kind: 'message',
      role: 'assistant',
      runId: event.run_id,
    });
    const key = partKeyOf(event.message_id, 'text');
    expect(useChatStore.getState().parts.get(key)).toBeUndefined(); // 未到帧：还在缓冲
    expect(deltaBuffers.channel(key, 'text').pendingChars).toBe(event.delta.length);

    flushAllChannels();
    expect(useChatStore.getState().parts.get(key)).toEqual({ text: event.delta, version: 1 });
  });

  it('message_delta kind=thinking → 思考通道独立累积', () => {
    const event = MessageDeltaEventSchema.parse({ ...messageDelta, kind: 'thinking', delta: '推理中' });

    HANDLERS.message_delta(event);
    flushAllChannels();

    expect(useChatStore.getState().parts.get(partKeyOf(MESSAGE_ID, 'thinking'))).toEqual({
      text: '推理中',
      version: 1,
    });
    expect(useChatStore.getState().parts.get(partKeyOf(MESSAGE_ID, 'text'))).toBeUndefined();
  });

  it('message_finished → 收口（finish_reason/structured）+ usage 入 costStore + 通道终态冲刷', () => {
    const event = MessageFinishedEventSchema.parse(messageFinished);
    HANDLERS.message_delta(MessageDeltaEventSchema.parse({ ...messageDelta, seq: event.seq - 1 }));

    HANDLERS.message_finished(event);

    const item = useChatStore.getState().byId.get(event.message_id);
    expect(item).toMatchObject({ kind: 'message', finishReason: 'stop' });
    if (item?.kind === 'message') {
      expect(item.structured?.status).toBe('ok');
      expect(item.structured?.data?.findings).toHaveLength(2);
    }
    expect(useCostStore.getState().sessionUsage).toEqual({ input_tokens: 5200, output_tokens: 1100 });
    // 终态冲刷：缓冲里的正文已落 store（无需等帧）
    expect(useChatStore.getState().parts.get(partKeyOf(event.message_id, 'text'))?.text).toBe(
      messageDelta.delta,
    );
    expect(deltaBuffers.channel(partKeyOf(event.message_id, 'text'), 'text').pendingChars).toBe(0);
  });
});

describe('handlers：工具卡族', () => {
  it('tool_call_started → 追加 running 卡片（tool/args/risk_level/agent）', () => {
    const event = ToolCallStartedEventSchema.parse(toolCallStarted);

    HANDLERS.tool_call_started(event);

    expect(useChatStore.getState().byId.get(event.tool_call_id)).toMatchObject({
      kind: 'toolcard',
      tool: 'execute',
      status: 'running',
      riskLevel: 'medium',
      args: { command: 'pytest -q tests/', timeout: 600 },
    });
    expect(useChatStore.getState().items).toEqual([event.tool_call_id]);
  });

  it('tool_call_finished（成功 + todos）→ 卡片补全 + todo 快照，且**不**重复插条目', () => {
    // 注意：started/finished 两个 fixture 的 tool_call_id 不同（同一会话里两次工具调用），
    // 这里对齐成同一次调用再断言「配对补全」语义。
    const finished = ToolCallFinishedEventSchema.parse(toolCallFinished);
    const started = ToolCallStartedEventSchema.parse({
      ...toolCallStarted,
      tool_call_id: finished.tool_call_id,
    });
    HANDLERS.tool_call_started(started);

    HANDLERS.tool_call_finished(finished);

    const card = useChatStore.getState().byId.get(finished.tool_call_id);
    expect(card).toMatchObject({
      kind: 'toolcard',
      status: 'success',
      durationMs: 120,
      exitCode: null,
      truncated: false,
      resultPreview: 'Todo list updated (4 items)',
    });
    expect(useChatStore.getState().items).toHaveLength(1); // 仍是同一条卡片条目
    expect(useTodoStore.getState().snapshot).toHaveLength(4);
    expect(useTodoStore.getState().snapshot.map((todo) => todo.status)).toEqual([
      'completed',
      'completed',
      'in_progress',
      'pending',
    ]);
  });

  it('tool_call_finished（失败）→ status=error + error 对象如实保留（AC-003-2/AC-004-4）', () => {
    const finished = ToolCallFinishedEventSchema.parse(toolCallFinishedError);
    HANDLERS.tool_call_started(
      ToolCallStartedEventSchema.parse({ ...toolCallStarted, tool_call_id: finished.tool_call_id }),
    );

    HANDLERS.tool_call_finished(finished);

    const card = useChatStore.getState().byId.get(finished.tool_call_id);
    expect(card).toMatchObject({ kind: 'toolcard', status: 'error' });
    if (card?.kind === 'toolcard') {
      expect(card.error?.code).toBe(finished.error?.code);
      expect(card.exitCode).toBe(finished.exit_code);
    }
  });

  it('tool_call_finished 无 todos（单步任务）→ 不动 todoStore（AC-005-3）', () => {
    const finished = ToolCallFinishedEventSchema.parse({ ...toolCallFinished, todos: null });

    HANDLERS.tool_call_started(ToolCallStartedEventSchema.parse(toolCallStarted));
    HANDLERS.tool_call_finished(finished);

    expect(useTodoStore.getState().snapshot).toEqual([]);
  });
});

describe('handlers：审批族（M3 预锚，M2 先落表）', () => {
  it('approval_request → pending 入表 + 卡片入流 + 会话 waiting_approval', () => {
    seedSession({ run_state: 'running' });
    const event = ApprovalRequestEventSchema.parse(approvalRequest);

    HANDLERS.approval_request(event);

    const entry = useApprovalStore.getState().byId.get(event.approval_id);
    expect(entry).toMatchObject({
      id: event.approval_id,
      tool: 'execute',
      riskLevel: 'high',
      reason: '命中危险命令规则：rm -rf',
      status: 'pending',
      toolCallId: event.tool_call_id,
    });
    expect(useApprovalStore.getState().pendingIds).toEqual([event.approval_id]);
    expect(useChatStore.getState().byId.get(event.approval_id)).toMatchObject({
      kind: 'approval',
      status: 'pending',
    });
    expect(useChatStore.getState().activeRun.state).toBe('waiting_approval');
    expect(useSessionStore.getState().list[0]?.run_state).toBe('waiting_approval');
  });

  it('approval_resolved（edit）→ 终态 + edited_args 回显 + 会话回 running', () => {
    const request = ApprovalRequestEventSchema.parse(approvalRequest);
    const resolved = ApprovalResolvedEventSchema.parse(approvalResolved);
    seedSession({ run_state: 'waiting_approval' });
    HANDLERS.approval_request(request);

    HANDLERS.approval_resolved(resolved);

    const entry = useApprovalStore.getState().byId.get(resolved.approval_id);
    expect(entry).toMatchObject({
      status: 'edited',
      decision: 'edit',
      scope: 'once',
      decidedBy: 'local',
    });
    expect(entry?.editedArgs).toEqual({ command: 'rm -rf dist/', timeout: 120 });
    expect(useApprovalStore.getState().pendingIds).toEqual([]);
    expect(useChatStore.getState().byId.get(resolved.approval_id)).toMatchObject({
      kind: 'approval',
      status: 'edited',
    });
    expect(useChatStore.getState().activeRun.state).toBe('running');
    expect(useSessionStore.getState().list[0]?.run_state).toBe('running');
  });
});

describe('handlers：diff 族', () => {
  it('diff_ready → review 快照 + 流内改动文件卡（+N/-N 摘要）', () => {
    const event = DiffReadyEventSchema.parse(diffReady);

    HANDLERS.diff_ready(event);

    const review = useDiffStore.getState().reviews.get(event.review_id);
    expect(review).toMatchObject({
      round: 2,
      additionsTotal: 17,
      deletionsTotal: 3,
      patchRef: `/api/v1/diff-reviews/${event.review_id}`,
      verdict: 'pending',
    });
    expect(review?.files).toHaveLength(2);
    expect(useChatStore.getState().byId.get(event.review_id)).toMatchObject({
      kind: 'diffcard',
      round: 2,
      fileCount: 2,
      additionsTotal: 17,
      deletionsTotal: 3,
      verdict: 'pending',
    });
  });

  it('diff_resolved（accepted）→ verdict 更新到 store 与卡片两处（AC-007-4）', () => {
    const ready = DiffReadyEventSchema.parse(diffReady);
    const resolved = DiffResolvedEventSchema.parse(diffResolved);
    HANDLERS.diff_ready(ready);

    HANDLERS.diff_resolved(resolved);

    expect(useDiffStore.getState().reviews.get(resolved.review_id)?.verdict).toBe('accepted');
    expect(useChatStore.getState().byId.get(resolved.review_id)).toMatchObject({
      kind: 'diffcard',
      verdict: 'accepted',
    });
    // 基线信息仍在（accepted 后基线的呈现由 review 快照承载）
    expect(useDiffStore.getState().reviews.get(resolved.review_id)?.baseline.kind).toBe('accepted');
  });
});

describe('handlers：沙箱与运行状态', () => {
  it('sandbox_output(stdout) → 终端缓冲（exec_id 归属），冲后落 terminalStore', () => {
    const event = SandboxOutputEventSchema.parse(sandboxOutput);

    HANDLERS.sandbox_output(event);
    expect(useTerminalStore.getState().execs.size).toBe(0); // 100ms 限频内不落库

    terminalBuffer.flush();
    const exec = useTerminalStore.getState().execs.get(event.exec_id ?? 'missing');
    expect(exec?.lines).toHaveLength(1);
    expect(exec?.lines[0]).toContain('test_auth.py');
    expect(useTerminalStore.getState().execOrder).toEqual([event.exec_id]);
  });

  it('sandbox_output(lifecycle) → terminalStore 相位 + costStore 计时基准，不进终端行缓冲', () => {
    const event = SandboxOutputEventSchema.parse(sandboxOutputLifecycle);

    HANDLERS.sandbox_output(event);

    expect(useTerminalStore.getState().lifecycle).toEqual({
      phase: 'paused',
      at: event.created_at,
      provider: 'bailian',
      reason: event.reason,
    });
    expect(useTerminalStore.getState().sandboxId).toBe(event.sandbox_id);
    expect(useCostStore.getState().sandboxPhase).toEqual({ phase: 'paused', at: event.created_at });
    expect(useTerminalStore.getState().execs.size).toBe(0);
  });

  it('sandbox_output(stdout) 缺 exec_id（B 档契约违规）→ 落 unattributed 桶而非丢弃', () => {
    const event = SandboxOutputEventSchema.parse({ ...sandboxOutput, exec_id: null });

    HANDLERS.sandbox_output(event);
    terminalBuffer.flush();

    expect(useTerminalStore.getState().execs.get('unattributed')?.lines).toHaveLength(1);
  });

  it('run_status(started) → activeRun running + 会话徽标 + usage_totals 入 costStore', () => {
    seedSession();
    const event = RunStatusEventSchema.parse({ ...runStatus, phase: 'started' });

    HANDLERS.run_status(event);

    expect(useChatStore.getState().activeRun).toMatchObject({ state: 'running', runId: event.run_id });
    expect(useSessionStore.getState().list[0]?.run_state).toBe('running');
    expect(useCostStore.getState().sessionUsage).toEqual({ input_tokens: 52340, output_tokens: 8210 });
  });

  it('run_status(error) → 终态收敛 idle + 全通道终态冲刷（后端 error 即终局，不再补发终态）', () => {
    seedSession({ run_state: 'running' });
    HANDLERS.run_status(RunStatusEventSchema.parse({ ...runStatus, phase: 'started' }));
    HANDLERS.message_delta(
      MessageDeltaEventSchema.parse({ ...messageDelta, seq: messageDelta.seq + 1 }),
    );
    const errorEvent = RunStatusEventSchema.parse({
      ...runStatus,
      phase: 'error',
      error: { code: 'rate_limited', message: '上游限流', retryable: true },
    });

    HANDLERS.run_status(errorEvent);

    expect(useChatStore.getState().activeRun).toMatchObject({
      state: 'idle',
      error: { code: 'rate_limited' },
    });
    expect(useSessionStore.getState().list[0]?.run_state).toBe('idle');
    // 终态冲刷（规则 10）：已收内容不丢，缓冲清空
    expect(useChatStore.getState().parts.get(partKeyOf(MESSAGE_ID, 'text'))?.text).toBe(
      messageDelta.delta,
    );
    expect(deltaBuffers.channel(partKeyOf(MESSAGE_ID, 'text'), 'text').pendingChars).toBe(0);
  });

  it('run_status(finished) → 收敛 idle + 全通道终态冲刷', () => {
    seedSession({ run_state: 'running' });
    HANDLERS.run_status(RunStatusEventSchema.parse({ ...runStatus, phase: 'started' }));
    HANDLERS.message_delta(MessageDeltaEventSchema.parse({ ...messageDelta, seq: messageDelta.seq + 1 }));

    HANDLERS.run_status(RunStatusEventSchema.parse(runStatus)); // phase=finished

    expect(useChatStore.getState().activeRun).toMatchObject({ state: 'idle', error: null });
    expect(useSessionStore.getState().list[0]?.run_state).toBe('idle');
    expect(useChatStore.getState().parts.get(partKeyOf(MESSAGE_ID, 'text'))?.text).toBe(
      messageDelta.delta,
    );
    expect(deltaBuffers.channel(partKeyOf(MESSAGE_ID, 'text'), 'text').pendingChars).toBe(0);
  });

  it('run_status(stopped) → 已收内容保留（AC-001-2）', () => {
    seedSession({ run_state: 'running' });
    HANDLERS.message_delta(MessageDeltaEventSchema.parse({ ...messageDelta, seq: messageDelta.seq + 1 }));

    HANDLERS.run_status(RunStatusEventSchema.parse({ ...runStatus, phase: 'stopped' }));

    expect(useChatStore.getState().activeRun.state).toBe('idle');
    expect(useChatStore.getState().parts.get(partKeyOf(MESSAGE_ID, 'text'))?.text).toBe(
      messageDelta.delta,
    );
    expect(useSessionStore.getState().list[0]?.run_state).toBe('idle');
  });

  it('run_status(subject=compact) 两态：started 建条目、finished 原地更新（AC-010-1）', () => {
    const finished = RunStatusEventSchema.parse(runStatusCompact);
    const started = RunStatusEventSchema.parse({ ...runStatusCompact, phase: 'started', seq: finished.seq - 1, compact: null });

    HANDLERS.run_status(started);
    const startedItem = useChatStore.getState().byId.get(`compact:${started.seq}`);
    expect(startedItem).toMatchObject({ kind: 'compact_note', phase: 'started', beforeTokens: null });
    expect(useChatStore.getState().compactItemId).toBe(`compact:${started.seq}`);

    HANDLERS.run_status(finished);

    const finishedItem = useChatStore.getState().byId.get(`compact:${started.seq}`);
    expect(finishedItem).toMatchObject({
      kind: 'compact_note',
      phase: 'finished',
      beforeTokens: 49970,
      afterTokens: 9836,
    });
    expect(useChatStore.getState().items.filter((id) => id.startsWith('compact:'))).toHaveLength(1);
    expect(useChatStore.getState().compactItemId).toBeNull();
  });

  it('run_status(subject=compact, finished) 单独到达（重放只见终态）也建条目', () => {
    const finished = RunStatusEventSchema.parse(runStatusCompact);

    HANDLERS.run_status(finished);

    expect(useChatStore.getState().byId.get(`compact:${finished.seq}`)).toMatchObject({
      kind: 'compact_note',
      phase: 'finished',
      beforeTokens: 49970,
      afterTokens: 9836,
    });
  });
});

describe('handlers：P1 占位与幂等', () => {
  it('browser_action → 忽略 + 单次告警，不写任何 store（P0 服务端不应产出）', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined);

    HANDLERS.browser_action({} as never);
    HANDLERS.browser_action({} as never);

    expect(useChatStore.getState().items).toEqual([]);
    expect(warnSpy).toHaveBeenCalledTimes(1); // 只告警一次，防日志刷屏
  });

  it('重复 message_created（同 seq 之外的重复投递）不重复插条目', () => {
    const event = MessageCreatedEventSchema.parse(messageCreated);

    HANDLERS.message_created(event);
    HANDLERS.message_created(event);

    expect(useChatStore.getState().items).toEqual([event.message_id]);
  });

  it('approval_resolved 先于 approval_request 到达（缺前态）→ 不凭空造卡片', () => {
    const resolved = ApprovalResolvedEventSchema.parse(approvalResolved);

    HANDLERS.approval_resolved(resolved);

    expect(useApprovalStore.getState().byId.size).toBe(0);
    expect(useChatStore.getState().items).toEqual([]);
  });
});
