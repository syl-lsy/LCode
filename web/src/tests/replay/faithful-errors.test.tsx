// @vitest-environment jsdom
/**
 * 回放 · 失败/边界如实呈现（AC-004-4；test-plan §2.4）：
 * 失败工具卡片不消失不美化（error 原样）；负例 fixtures（A 档 13 条）经 dispatcher
 * 拒收：不崩、不写坏 store、validationErrorCount 打点（A4 §5.2 校验失败策略）。
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import messageDeltaExtraField from '@contracts-fixtures/events/invalid/message_delta__extra_field.json';
import messageDeltaMissingDelta from '@contracts-fixtures/events/invalid/message_delta__missing_delta.json';
import messageDeltaNegativeSeq from '@contracts-fixtures/events/invalid/message_delta__negative_seq.json';
import messageDeltaSeqString from '@contracts-fixtures/events/invalid/message_delta__seq_string.json';
import runStatusBadCode from '@contracts-fixtures/events/invalid/run_status__run_error_bad_code.json';
import toolFinishedBadStatus from '@contracts-fixtures/events/invalid/tool_call_finished__bad_status_enum.json';
import toolFinishedBadErrorCode from '@contracts-fixtures/events/invalid/tool_call_finished__error_bad_code.json';
import unknownEventType from '@contracts-fixtures/events/invalid/unknown_event_type.json';
import toolCallFinishedError from '@contracts-fixtures/events/valid/tool_call_finished_error.json';

import { useChatStore } from '@/features/chat/store';
import { useConnStore } from '@/stores/connStore';
import { getDispatcherStats } from '@/events/dispatcher';
import { MessageList } from '@/features/chat/components/MessageList';
import { RunErrorBanner } from '@/features/chat/components/RunErrorBanner';

import { delta, eventBase, feedFixtures } from './helpers';
import '../../tests/test-utils';

const SESSION = '0192f0a1-1111-4a1a-9c2d-000000000001';

describe('replay/faithful-errors：失败如实呈现 + 负例拒收（A 档）', () => {
  it('失败卡片保留在流中：status/error/duration 原样（不消失不美化）', async () => {
    const user = userEvent.setup();
    feedFixtures([
      eventBase(120, 'tool_call_started', {
        tool_call_id: toolCallFinishedError.tool_call_id,
        tool: 'tavily_search',
        args: { query: 'lcode' },
      }),
      toolCallFinishedError,
    ]);
    render(<MessageList sessionId={SESSION} />);
    expect(screen.getByTestId('tool-card')).toHaveAttribute('data-status', 'error');
    await user.click(screen.getByTestId('tool-card-toggle'));
    expect(screen.getByTestId('tool-error-detail')).toHaveTextContent('timeout');
    expect(screen.getByTestId('tool-error-detail')).toHaveTextContent(
      'tavily_search 30s 超时；操作可能仍在服务端运行，重试可能重复执行',
    );
  });

  it('A 档负例：zod 校验失败丢弃 + validationErrors 打点，store 不被写入', () => {
    const invalids = [
      messageDeltaExtraField,
      messageDeltaMissingDelta,
      messageDeltaNegativeSeq,
      messageDeltaSeqString,
      runStatusBadCode,
      toolFinishedBadStatus,
      toolFinishedBadErrorCode,
    ];
    feedFixtures(invalids);

    expect(getDispatcherStats().droppedInvalid).toBeGreaterThanOrEqual(invalids.length);
    expect(useConnStore.getState().validationErrorCount).toBeGreaterThanOrEqual(invalids.length);
    // store 未被坏数据污染：无消息条目、无运行态
    expect(useChatStore.getState().items).toEqual([]);
    expect(useChatStore.getState().activeRun.state).toBe('idle');
  });

  it('unknown_event_type 走白名单忽略路径（unknownTypes 打点，非校验失败）', () => {
    feedFixtures([unknownEventType]);
    expect(getDispatcherStats().unknownTypes).toBeGreaterThanOrEqual(1);
    expect(useChatStore.getState().items).toEqual([]);
  });

  it('坏事件夹在好事件之间：好事件照常消费（管线不因单帧失效）', () => {
    feedFixtures([messageDeltaSeqString]); // 丢弃
    feedFixtures([
      eventBase(1, 'message_created', { message_id: 'm-ok', content: '正常消息' }),
    ]);
    expect(useChatStore.getState().items).toEqual(['m-ok']);
  });

  it('回放 run_status phase=error（带 error 对象）：横幅出现、run 收敛 idle、既有消息不丢（F1a/B2）', () => {
    // 合法序列：started → user 回声 → assistant 增量 → run 终态 error（契约 phase=error 必带 RunErrorObj）
    feedFixtures([
      eventBase(200, 'run_status', { subject: 'run', phase: 'started' }),
      eventBase(201, 'message_created', {
        message_id: 'm-user-err',
        role: 'user',
        content: '帮我修一个登录超时的 bug',
      }),
      delta({ seq: 202, messageId: 'm-asst-err', delta: '已收到，正在定位问题' }),
      eventBase(203, 'run_status', {
        subject: 'run',
        phase: 'error',
        error: { code: 'model_error', message: '上游模型调用失败', retryable: true },
      }),
    ]);

    render(
      <>
        <RunErrorBanner />
        <MessageList sessionId={SESSION} />
      </>,
    );

    // 横幅出现：message/code/retryable 如实呈现
    expect(screen.getByTestId('run-error-banner')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByTestId('run-error-message')).toHaveTextContent('上游模型调用失败');
    expect(screen.getByTestId('run-error-code')).toHaveTextContent('model_error');
    expect(screen.getByTestId('run-error-retryable')).toHaveTextContent('可重试');

    // run 收敛 idle 且 error 保留在 activeRun（终态口径与 finished/stopped 一致）
    expect(useChatStore.getState().activeRun.state).toBe('idle');
    expect(useChatStore.getState().activeRun.error).toMatchObject({
      code: 'model_error',
      retryable: true,
    });

    // 既有消息不丢：user 回声与 assistant 已收增量都还在流中
    expect(screen.getByTestId('message-user')).toHaveTextContent('帮我修一个登录超时的 bug');
    expect(screen.getByTestId('message-assistant')).toBeInTheDocument();
    expect(screen.getByTestId('message-assistant')).toHaveTextContent('已收到，正在定位问题');
  });
});
