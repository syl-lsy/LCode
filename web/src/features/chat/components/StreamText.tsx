import { partKeyOf, useChatStore } from '../store';

import { Markdown } from '@/components/Markdown';

/**
 * 流式正文（A4 §8.1 StreamText 行）：订阅 part flushed 文本（partKey =
 * `message_id:text`），驱动 Markdown 限频渲染（§6.2/§6.3）。
 *
 * selector 纪律（§3.3）：只订阅该 part 的 text（字符串，引用稳定）——其它 part
 * 更新不触发本组件重渲。停止/断线后已收内容保留在 store（AC-001-2），本组件
 * 无条件渲染现有文本——停止不是清空。
 */
export interface StreamTextProps {
  messageId: string;
  /** 是否仍在生成（run 未收敛或消息未收口）：空文本时显示生成中占位 */
  streaming: boolean;
}

export function StreamText({ messageId, streaming }: StreamTextProps) {
  const text = useChatStore((s) => s.parts.get(partKeyOf(messageId, 'text'))?.text ?? '');

  if (text === '' && streaming) {
    return (
      /* .loading-row（task-conversation.css，来源 task-conversation.html 加载行） */
      <div data-testid="stream-loading" className="loading-row">
        <span className="spinner flex-none" aria-hidden="true" />
        正在生成…
      </div>
    );
  }
  if (text === '') return null;
  return <Markdown text={text} />;
}
