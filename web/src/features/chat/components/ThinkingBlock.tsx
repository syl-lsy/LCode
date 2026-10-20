import { partKeyOf, useChatStore } from '../store';

import { Markdown } from '@/components/Markdown';

/**
 * 思考折叠块（A4 §8.1 ThinkingBlock 行；message_delta kind=thinking 通道）。
 *
 * 默认折叠（原型 details.think，F3 总折叠的组成之一）；思考通道 k=0.25 加速直出
 * （§6.1，缓冲侧已定），本组件只展示。摘要行给字符量，正文随 details 展开可见。
 */
export interface ThinkingBlockProps {
  messageId: string;
}

export function ThinkingBlock({ messageId }: ThinkingBlockProps) {
  const text = useChatStore((s) => s.parts.get(partKeyOf(messageId, 'thinking'))?.text ?? '');
  if (text === '') return null;

  return (
    <details data-testid="thinking-block" className="mb-3 rounded-md border border-border bg-surface">
      <summary className="flex cursor-pointer list-none items-center gap-1.5 px-3 py-2 text-[length:var(--text-ui-xs)] text-foreground-subtle [&::-webkit-details-marker]:hidden">
        <span aria-hidden="true" className="text-foreground-subtlest">
          ▸
        </span>
        思考 · {text.length} 字
      </summary>
      <div className="border-t border-border px-3 py-2 text-[length:var(--text-ui-xs)] italic leading-[1.7] text-foreground-subtle">
        <Markdown text={text} />
      </div>
    </details>
  );
}
