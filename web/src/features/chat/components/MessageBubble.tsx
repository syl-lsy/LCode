import { useState } from 'react';

import type { ChatMessageItem } from '../store';

import { StreamText } from './StreamText';
import { ThinkingBlock } from './ThinkingBlock';

/**
 * 消息气泡（A4 §8.1 MessageBubble 行）。
 *
 * user：右对齐灰卡（--color-msg-user-bg，F1，max-w ~75%）+ 复制（编辑 P1 分叉另立）；
 * assistant：ThinkingBlock（思考通道）+ StreamText（正文通道）+ 收口标注
 * （finish_reason=error/cancelled 如实呈现，不美化——AC-004-4）。
 */

function UserBubble({ item }: { item: ChatMessageItem }) {
  const [copied, setCopied] = useState(false);
  const content = item.content ?? '';

  const copy = (): void => {
    const clip = typeof navigator !== 'undefined' ? navigator.clipboard : undefined;
    if (clip !== undefined) {
      void clip.writeText(content).then(
        () => setCopied(true),
        () => setCopied(false),
      );
    } else {
      setCopied(true); // 无 clipboard 环境（旧内嵌 webview）：给出已复制反馈但不承诺
    }
  };

  return (
    <div
      data-testid="message-user"
      data-message-id={item.id}
      className="mb-3.5 ml-auto max-w-[75%] rounded-lg border border-msg-user-border bg-msg-user-bg px-3.5 py-2.5"
    >
      <p className="text-[length:var(--text-ui-sm)] leading-[1.65] whitespace-pre-wrap text-foreground-strong">
        {content}
      </p>
      <div className="mt-1.5 flex gap-0.5 opacity-85">
        <button
          type="button"
          data-testid="message-copy"
          className="rounded-sm px-1.5 py-0.5 text-[length:var(--text-ui-2xs)] text-foreground-subtle hover:bg-surface-hover hover:text-foreground"
          onClick={copy}
        >
          {copied ? '已复制' : '复制'}
        </button>
      </div>
    </div>
  );
}

function AssistantBubble({ item }: { item: ChatMessageItem }) {
  const finished = item.finishReason !== undefined;
  return (
    <div data-testid="message-assistant" data-message-id={item.id} className="mb-3.5">
      <ThinkingBlock messageId={item.id} />
      <StreamText messageId={item.id} streaming={!finished} />
      {item.finishReason === 'error' && (
        <p className="mt-1 text-[length:var(--text-ui-2xs)] text-destructive" data-testid="message-error-note">
          生成失败（finish_reason=error），已收内容如实保留
        </p>
      )}
      {item.finishReason === 'cancelled' && (
        <p className="mt-1 text-[length:var(--text-ui-2xs)] text-foreground-subtlest" data-testid="message-cancelled-note">
          已停止生成，以上为已收内容
        </p>
      )}
    </div>
  );
}

export function MessageBubble({ item }: { item: ChatMessageItem }) {
  return item.role === 'user' ? (
    <UserBubble item={item} />
  ) : (
    <AssistantBubble item={item} />
  );
}
