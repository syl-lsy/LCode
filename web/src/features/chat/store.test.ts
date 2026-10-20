/**
 * chatStore 单测（A4 §3.2/§3.3/§8.2）。
 *
 * 重点：不可变边界（items append-only / parts 只换对象引用 / byId 条目按事件替换）、
 * assistant 条目幂等建立、activeRun 状态机（started→running；finished/stopped/error
 * 同为终态→idle）、compact 两态单条目、generation 非追加输入代际。
 */
import { beforeEach, describe, expect, it } from 'vitest';

import { partKeyOf, useChatStore } from './store';

const MSG = '0192f0a1-4444-4a1a-9c2d-000000000004';

beforeEach(() => {
  useChatStore.getState().reset();
});

describe('chatStore：条目模型', () => {
  it('appendUserMessage：user 消息入 items + byId（正文随条目）', () => {
    useChatStore.getState().appendUserMessage({
      id: 'u1',
      content: '你好',
      createdAt: '2026-10-18T00:00:00+00:00',
      runId: 'run-1',
    });

    expect(useChatStore.getState().items).toEqual(['u1']);
    expect(useChatStore.getState().byId.get('u1')).toMatchObject({
      kind: 'message',
      role: 'user',
      content: '你好',
      runId: 'run-1',
    });
  });

  it('appendUserMessage 幂等：重复投递不重复插条目', () => {
    const input = {
      id: 'u1',
      content: '你好',
      createdAt: '2026-10-18T00:00:00+00:00',
      runId: null,
    };
    useChatStore.getState().appendUserMessage(input);
    useChatStore.getState().appendUserMessage(input);
    expect(useChatStore.getState().items).toEqual(['u1']);
  });

  it('ensureAssistantMessage：首个 delta 建条目，后续 delta 不重复', () => {
    const input = { id: MSG, createdAt: '2026-10-18T00:00:01+00:00', runId: 'run-1' };
    useChatStore.getState().ensureAssistantMessage(input);
    useChatStore.getState().ensureAssistantMessage(input);

    expect(useChatStore.getState().items).toEqual([MSG]);
    expect(useChatStore.getState().byId.get(MSG)).toMatchObject({ kind: 'message', role: 'assistant' });
  });

  it('finishMessage：收口字段落在既有条目上；条目不存在时不造条目', () => {
    useChatStore.getState().ensureAssistantMessage({ id: MSG, createdAt: 't', runId: null });
    useChatStore.getState().finishMessage({ id: MSG, finishReason: 'stop', structured: null });
    expect(useChatStore.getState().byId.get(MSG)).toMatchObject({ finishReason: 'stop' });

    useChatStore.getState().finishMessage({ id: 'ghost', finishReason: 'error', structured: null });
    expect(useChatStore.getState().items).toEqual([MSG]);
  });
});

describe('chatStore：工具卡 / 审批 / diff 卡 / compact 条目', () => {
  it('toolcard 两态：running → success/error 补全不换 id；缺 started 不造卡', () => {
    useChatStore.getState().appendToolCard({
      id: 'call_1',
      tool: 'execute',
      args: { command: 'pytest' },
      riskLevel: 'medium',
      createdAt: 't1',
    });
    useChatStore.getState().completeToolCall({
      id: 'call_1',
      status: 'success',
      durationMs: 120,
      exitCode: 0,
      resultPreview: 'ok',
    });

    expect(useChatStore.getState().items).toEqual(['call_1']);
    expect(useChatStore.getState().byId.get('call_1')).toMatchObject({
      kind: 'toolcard',
      status: 'success',
      durationMs: 120,
      exitCode: 0,
    });

    useChatStore.getState().completeToolCall({
      id: 'ghost',
      status: 'error',
      durationMs: 1,
      error: { code: 'timeout', message: 'x', retryable: null },
    });
    expect(useChatStore.getState().items).toEqual(['call_1']);
  });

  it('approval 条目：pending 入流，终态原地更新（状态机折叠不重复）', () => {
    useChatStore.getState().appendApprovalItem({ id: 'ap-1', tool: 'execute', status: 'pending', createdAt: 't' });
    useChatStore.getState().appendApprovalItem({ id: 'ap-1', tool: 'execute', status: 'pending', createdAt: 't' });
    expect(useChatStore.getState().items).toEqual(['ap-1']);

    useChatStore.getState().setApprovalItemStatus('ap-1', 'approved');
    expect(useChatStore.getState().byId.get('ap-1')).toMatchObject({ kind: 'approval', status: 'approved' });
  });

  it('diffcard：pending 入流 + verdict 原地更新', () => {
    useChatStore.getState().appendDiffCard({
      id: 'rev-1',
      round: 2,
      fileCount: 2,
      additionsTotal: 17,
      deletionsTotal: 3,
      createdAt: 't',
    });
    useChatStore.getState().setDiffCardVerdict('rev-1', 'accepted');

    expect(useChatStore.getState().byId.get('rev-1')).toMatchObject({
      kind: 'diffcard',
      verdict: 'accepted',
    });
  });

  it('compact 两态：started 建条目 → finished 原地更新并清 active 指针（单条目）', () => {
    useChatStore.getState().beginCompact({ id: 'compact:1', at: 't1' });
    expect(useChatStore.getState().items).toEqual(['compact:1']);
    expect(useChatStore.getState().compactItemId).toBe('compact:1');

    useChatStore.getState().finishCompact({ id: 'compact:1', at: 't2', beforeTokens: 100, afterTokens: 20 });
    expect(useChatStore.getState().byId.get('compact:1')).toMatchObject({
      kind: 'compact_note',
      phase: 'finished',
      beforeTokens: 100,
      afterTokens: 20,
    });
    expect(useChatStore.getState().compactItemId).toBeNull();
    expect(useChatStore.getState().items).toEqual(['compact:1']);
  });
});

describe('chatStore：activeRun 状态机（run 终态收敛）', () => {
  it('started → running；finished/stopped → idle；waiting_approval 由审批事件驱动', () => {
    useChatStore.getState().applyRunStatus({
      subject: 'run',
      phase: 'started',
      runId: 'run-1',
      createdAt: 't',
    });
    expect(useChatStore.getState().activeRun).toMatchObject({ state: 'running', runId: 'run-1' });

    useChatStore.getState().noteApprovalPending();
    expect(useChatStore.getState().activeRun.state).toBe('waiting_approval');

    useChatStore.getState().noteApprovalSettled();
    expect(useChatStore.getState().activeRun.state).toBe('running');

    useChatStore.getState().applyRunStatus({ subject: 'run', phase: 'stopped', runId: 'run-1', createdAt: 't' });
    expect(useChatStore.getState().activeRun).toMatchObject({ state: 'idle', error: null });
  });

  it('error 即终态：收敛 idle 且保留 error（与 finished/stopped 同口径）', () => {
    useChatStore.getState().applyRunStatus({ subject: 'run', phase: 'started', runId: 'run-1', createdAt: 't' });
    useChatStore.getState().applyRunStatus({
      subject: 'run',
      phase: 'error',
      runId: 'run-1',
      createdAt: 't',
      error: { code: 'rate_limited', message: '限流', retryable: true },
    });

    expect(useChatStore.getState().activeRun).toMatchObject({
      state: 'idle',
      runId: 'run-1',
      error: { code: 'rate_limited' },
    });
  });

  it('error 收敛后到达 finished（防御）：保持 idle，error 被后续终态覆盖', () => {
    useChatStore.getState().applyRunStatus({
      subject: 'run',
      phase: 'error',
      runId: 'run-1',
      createdAt: 't',
      error: { code: 'rate_limited', message: '限流', retryable: true },
    });
    useChatStore.getState().applyRunStatus({ subject: 'run', phase: 'finished', runId: 'run-1', createdAt: 't' });
    expect(useChatStore.getState().activeRun).toMatchObject({ state: 'idle', error: null });
  });

  it('subject=compact 不进 activeRun 状态机', () => {
    useChatStore.getState().applyRunStatus({
      subject: 'compact',
      phase: 'started',
      runId: null,
      createdAt: 't',
    });
    expect(useChatStore.getState().activeRun.state).toBe('idle');
  });

  it('markRunEnded（F12 RUN_NOT_ACTIVE 兜底）：running/waiting_approval → idle 保留 runId；已 idle 幂等不换引用', () => {
    useChatStore.getState().applyRunStatus({ subject: 'run', phase: 'started', runId: 'run-9', createdAt: 't' });
    useChatStore.getState().markRunEnded();
    expect(useChatStore.getState().activeRun).toEqual({
      runId: 'run-9',
      state: 'idle',
      startedAt: null,
      error: null,
    });

    // 审批窗口期同样收敛（stop 只可能在 running/waiting_approval 两个非 idle 态被点击）
    useChatStore.getState().applyRunStatus({ subject: 'run', phase: 'started', runId: 'run-10', createdAt: 't' });
    useChatStore.getState().noteApprovalPending();
    useChatStore.getState().markRunEnded();
    expect(useChatStore.getState().activeRun).toMatchObject({ state: 'idle', runId: 'run-10' });

    const snapshot = useChatStore.getState().activeRun;
    useChatStore.getState().markRunEnded(); // 已 idle：幂等，引用不变（不触发多余渲染）
    expect(useChatStore.getState().activeRun).toBe(snapshot);
  });
});

describe('chatStore：parts 与 generation（§3.3）', () => {
  it('partKeyOf = `${message_id}:${kind}`；appendPart 累加且 version 递增、对象引用换新', () => {
    expect(partKeyOf(MSG)).toBe(`${MSG}:text`);
    expect(partKeyOf(MSG, 'thinking')).toBe(`${MSG}:thinking`);

    const key = partKeyOf(MSG);
    useChatStore.getState().appendPart(key, '第一');
    const first = useChatStore.getState().parts.get(key);
    expect(first).toEqual({ text: '第一', version: 1 });

    useChatStore.getState().appendPart(key, '段');
    const second = useChatStore.getState().parts.get(key);
    expect(second).toEqual({ text: '第一段', version: 2 });
    expect(second).not.toBe(first); // 不可变边界：换对象引用
  });

  it('setPart 整段替换（batch 路径），version 继续递增', () => {
    const key = partKeyOf(MSG);
    useChatStore.getState().appendPart(key, '旧');
    useChatStore.getState().setPart(key, '新的完整文本');
    expect(useChatStore.getState().parts.get(key)).toEqual({ text: '新的完整文本', version: 2 });
  });

  it('bumpGeneration 递增（非追加输入使渲染缓存失效，规则 18）', () => {
    const before = useChatStore.getState().generation;
    useChatStore.getState().bumpGeneration();
    expect(useChatStore.getState().generation).toBe(before + 1);
  });
});
