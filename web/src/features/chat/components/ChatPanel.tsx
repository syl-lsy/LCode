/**
 * 中栏聊天流空壳（A4 §2.1 features/chat）。
 * M2 任务 5 接入：MessageList/MessageBubble/StreamText/ThinkingBlock/Composer +
 * chatStore + useStickToBottom（AC-001/AC-003-1 流式渲染）；
 * 作为消息流容器，chat 是唯一允许跨 feature import 卡片组件的例外（A4 §2.2）。
 */
export function ChatPanel() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-1">
      <span className="font-mono text-[length:var(--text-ui-xs)] text-foreground-subtle">
        chat · 消息流
      </span>
      <span className="font-mono text-[length:var(--text-ui-2xs)] text-foreground-subtlest">
        M2 任务 5 接入（SSE 事件管线 = 任务 4）
      </span>
    </div>
  );
}
