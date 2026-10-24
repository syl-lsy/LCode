import { partKeyOf, useChatStore } from '../store';

import { Icon } from '@/components/Icon';
import { Markdown } from '@/components/Markdown';

/**
 * 思考折叠块（A4 §8.1 ThinkingBlock 行；message_delta kind=thinking 通道）。
 *
 * 原型 details.think（F3 总折叠的组成之一）：默认折叠、摘要行 + 斜体正文；
 * 思考通道 k=0.25 加速直出（§6.1，缓冲侧已定），本组件只展示。摘要行给字符量，
 * 正文随 details 展开可见。
 */
export interface ThinkingBlockProps {
  messageId: string;
}

export function ThinkingBlock({ messageId }: ThinkingBlockProps) {
  const text = useChatStore((s) => s.parts.get(partKeyOf(messageId, 'thinking'))?.text ?? '');
  if (text === '') return null;

  return (
    <details data-testid="thinking-block" className="think">
      <summary>
        <Icon name="chevron-down" size={11} /> 思考 · {text.length} 字
      </summary>
      {/* .body（task-conversation.css）：斜体弱色正文 */}
      <div className="body">
        <Markdown text={text} />
      </div>
    </details>
  );
}
