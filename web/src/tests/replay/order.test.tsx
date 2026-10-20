// @vitest-environment jsdom
/**
 * 回放 · 顺序与幂等（AC-003-4 / 流式用例 2；test-plan §2.3）：
 * 事件序列回放顺序一致；同 seq 重放不重复不丢（dispatcher 水位幂等）；
 * fixtures（契约同源）回放与实时派发产出一致。
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import messageCreated from '@contracts-fixtures/events/valid/message_created.json';
import toolStarted from '@contracts-fixtures/events/valid/tool_call_started.json';

import { useChatStore } from '@/features/chat/store';
import { MessageList } from '@/features/chat/components/MessageList';

import { delta, eventBase, feedFixtures, feedRaw } from './helpers';
import '../../tests/test-utils';

const SESSION = '0192f0a1-1111-4a1a-9c2d-000000000001';
const MSG = '0192f0a1-4444-4a1a-9c2d-000000000004';

describe('replay/order：回放顺序与同 seq 幂等', () => {
  it('事件按发生顺序渲染：user 消息 → 工具卡 → 流式文本（AC-003-4）', () => {
    feedFixtures([
      messageCreated,
      toolStarted,
      delta({ seq: 118, messageId: MSG, delta: '已定位为 token 过期未刷新，' }),
      delta({ seq: 119, messageId: MSG, delta: '建议补刷新逻辑。' }),
    ]);

    render(<MessageList sessionId={SESSION} />);

    const userCard = screen.getByTestId('message-user');
    const toolCard = screen.getByTestId('tool-card');
    const assistantCard = screen.getByTestId('message-assistant');
    // DOM 顺序 = 事件发生顺序
    expect(userCard.compareDocumentPosition(toolCard) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(toolCard.compareDocumentPosition(assistantCard) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByTestId('message-scroll')).toHaveTextContent('已定位为 token 过期未刷新，建议补刷新逻辑。');
  });

  it('同 seq 重放不重复不丢：user 消息与 delta 只落一次', () => {
    feedFixtures([messageCreated]);
    feedFixtures([messageCreated]); // 整段重放（断线重连场景）
    expect(useChatStore.getState().items).toEqual([messageCreated.message_id as string]);

    feedRaw(delta({ seq: 118, messageId: MSG, delta: '第一段' }));
    feedRaw(delta({ seq: 118, messageId: MSG, delta: '第一段' })); // 同 seq → duplicate 跳过
    feedRaw(delta({ seq: 119, messageId: MSG, delta: '第二段' }));

    const text = useChatStore.getState().parts.get(`${MSG}:text`)?.text ?? '';
    expect(text).toBe('第一段第二段');

    render(<MessageList sessionId={SESSION} />);
    expect(screen.getByTestId('message-scroll')).toHaveTextContent('第一段第二段');
    expect(screen.getAllByTestId('message-user')).toHaveLength(1);
  });

  it('乱序到达（seq 倒退）不崩溃且不覆盖已收内容（契约跨 run 场景的最宽松路径）', () => {
    feedRaw(delta({ seq: 120, messageId: MSG, delta: '新段' }));
    feedRaw(eventBase(3, 'message_created', { message_id: 'm-old', content: '旧消息' }));
    feedRaw(delta({ seq: 121, messageId: MSG, delta: '续' }));
    // seq 3 低于水位 → duplicate 跳过，store 不被旧事件污染
    expect(useChatStore.getState().byId.has('m-old')).toBe(false);
    expect(useChatStore.getState().parts.get(`${MSG}:text`)?.text).toBe('新段续');
  });
});
