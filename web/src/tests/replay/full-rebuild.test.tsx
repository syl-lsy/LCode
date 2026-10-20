// @vitest-environment jsdom
/**
 * 回放 · 全量重建（AC-004-1；test-plan §2.4）：
 * 契约 fixtures 按 seq 升序整体回放（等价服务端首连全量重放）后，消息流/终端/成本/
 * 审批/压缩状态行/diff 卡与实时流一致——「刷新恢复 = 重放重建 UI」（A4 §4.2 路径 A）。
 * 回放两遍幂等（断线重连不重复不丢，规则 24）。
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import approvalRequest from '@contracts-fixtures/events/valid/approval_request.json';
import approvalResolved from '@contracts-fixtures/events/valid/approval_resolved.json';
import diffReady from '@contracts-fixtures/events/valid/diff_ready.json';
import diffResolved from '@contracts-fixtures/events/valid/diff_resolved.json';
import messageCreated from '@contracts-fixtures/events/valid/message_created.json';
import messageDelta from '@contracts-fixtures/events/valid/message_delta.json';
import messageFinished from '@contracts-fixtures/events/valid/message_finished.json';
import runStatus from '@contracts-fixtures/events/valid/run_status.json';
import runStatusCompact from '@contracts-fixtures/events/valid/run_status_compact.json';
import sandboxLifecycle from '@contracts-fixtures/events/valid/sandbox_output_lifecycle.json';
import sandboxOutput from '@contracts-fixtures/events/valid/sandbox_output.json';
import toolCallFinishedError from '@contracts-fixtures/events/valid/tool_call_finished_error.json';
import toolCallFinishedTodos from '@contracts-fixtures/events/valid/tool_call_finished.json';
import toolCallStarted from '@contracts-fixtures/events/valid/tool_call_started.json';

import { useApprovalStore } from '@/features/approval/store';
import { useChatStore } from '@/features/chat/store';
import { useCostStore } from '@/features/costs/store';
import { MessageList } from '@/features/chat/components/MessageList';
import { useTodoStore } from '@/features/todo/store';
import { useDiffStore, useTerminalStore } from '@/features/workbench/store';
import { TerminalView } from '@/features/workbench/terminal/TerminalView';

import { eventBase, feedFixtures } from './helpers';
import '../../tests/test-utils';

const SESSION = '0192f0a1-1111-4a1a-9c2d-000000000001';

/** 完整回放序列（严格 seq 升序：101→160；缺失的配对由 eventBase 自造补齐）。 */
const FULL_SEQUENCE = [
  messageCreated, // 101 user 回声
  toolCallStarted, // 105 execute 卡（call_a1b2c3）
  toolCallFinishedTodos, // 110 write_todos 快照（独立 tool_call_id：无卡只进 todoStore）
  sandboxOutput, // 112 stdout（exec call_a1b2c3，pytest 进度）
  messageDelta, // 118 text delta
  eventBase(120, 'tool_call_started', {
    // 为 121 的错误 finished 补配 started（fixtures 的 error 是独立调用）
    tool_call_id: toolCallFinishedError.tool_call_id,
    tool: 'tavily_search',
    args: { query: 'lcode m2' },
  }),
  toolCallFinishedError, // 121 失败卡（timeout）
  approvalRequest, // 126 审批 pending
  approvalResolved, // 130 edit 决策回声
  messageFinished, // 131 收口（usage 5200/1100）
  diffReady, // 140 diff 快照 + 改动卡
  diffResolved, // 145 accepted 裁决
  sandboxLifecycle, // 150 paused
  runStatusCompact, // 155 compact finished
  runStatus, // 160 run finished（usage_totals 52340/8210）
];

describe('replay/full-rebuild：事件序列重建完整 UI 状态', () => {
  it('回放后各面板与实时流一致（消息流/终端/成本/审批/压缩/diff/运行态）', () => {
    feedFixtures(FULL_SEQUENCE);

    render(<MessageList sessionId={SESSION} />);
    render(<TerminalView />);

    // 消息流：user + 三张工具卡（execute 成功、tavily 失败、write_todos 无卡）+ assistant
    expect(screen.getAllByTestId('message-user')).toHaveLength(1);
    expect(screen.getByTestId('message-scroll')).toHaveTextContent(messageDelta.delta);
    expect(screen.getAllByTestId('tool-card')).toHaveLength(2);
    expect(screen.getByTestId('tool-error-code')).toHaveTextContent('timeout');
    // 审批行终态（edit → edited 后允许）
    expect(screen.getByTestId('approval-row')).toHaveAttribute('data-status', 'edited');
    // 改动卡 accepted（AC-007-4 呈现）
    expect(screen.getByTestId('diff-card')).toHaveAttribute('data-verdict', 'accepted');
    // compact 完成态行（AC-010-1）
    expect(screen.getByTestId('compact-note')).toHaveAttribute('data-phase', 'finished');
    expect(screen.getByTestId('compact-note')).toHaveTextContent('49,970');

    // 终端：exec 标注 + paused 徽标
    expect(screen.getByTestId('terminal-exec')).toHaveAttribute('data-exec-id', sandboxOutput.exec_id);
    expect(screen.getByTestId('terminal-lifecycle')).toHaveAttribute('data-phase', 'paused');

    // 运行态收敛（run_status finished → idle；AC-001-2 状态机）
    expect(useChatStore.getState().activeRun.state).toBe('idle');
    expect(useChatStore.getState().compactItemId).toBeNull();

    // 成本：message_finished(5200/1100) + run_status.usage_totals(52340/8210) 累计（AC-015-1）
    expect(useCostStore.getState().sessionUsage.input_tokens).toBe(57540);
    expect(useCostStore.getState().sessionUsage.output_tokens).toBe(9310);

    // 各领域 store 落地（写者唯一：dispatcher）
    expect(useTodoStore.getState().snapshot).toHaveLength(4);
    expect(useApprovalStore.getState().pendingIds).toEqual([]); // 已 resolved 出挂起表
    expect(useApprovalStore.getState().byId.get(approvalResolved.approval_id)).toMatchObject({
      status: 'edited',
      editedArgs: approvalResolved.edited_args,
    });
    expect(useDiffStore.getState().reviews.get(diffReady.review_id)).toMatchObject({
      round: 2,
      verdict: 'accepted',
    });
    expect(useTerminalStore.getState().lifecycle?.phase).toBe('paused');
  });

  it('重放两遍（断线重连重复回放）幂等：条目与终态不变', () => {
    feedFixtures(FULL_SEQUENCE);
    const itemsAfterFirst = [...useChatStore.getState().items];
    const usageAfterFirst = { ...useCostStore.getState().sessionUsage };
    feedFixtures(FULL_SEQUENCE); // 同 seq 重放 → 全部 duplicate 跳过
    expect(useChatStore.getState().items).toEqual(itemsAfterFirst);
    expect(useCostStore.getState().sessionUsage).toEqual(usageAfterFirst);
  });
});
