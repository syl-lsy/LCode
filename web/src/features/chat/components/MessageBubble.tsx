import { useEffect, useRef, useState } from 'react';

import { Icon } from '@/components/Icon';
import { partKeyOf, useChatStore, type ChatMessageItem } from '../store';

import { StreamText } from './StreamText';
import { ThinkingBlock } from './ThinkingBlock';

/**
 * 消息气泡（A4 §8.1 MessageBubble 行；F1 用户消息改型 + F4 完成态操作栏）。
 *
 * user：右对齐圆角卡（.msg-user，--color-msg-user-bg，max-w ~75%，R3§D）；卡下操作
 * = 复制（真实可用）/ 编辑（仅对最新一条渲染；编辑态无后端支撑，点击轻提示占位）；
 * 长消息默认 2 行截断（.clamp，R9），「展开/收起」chip 切换。
 * assistant：ThinkingBlock（思考通道）+ StreamText（正文通道）+ 收口标注
 * （finish_reason=error/cancelled 如实呈现，不美化——AC-004-4）；正常收口后出
 * F4 完成态操作栏 = 复制/赞/踩/分叉/钩子 + 时间戳（「撤销」仅在改动文件卡内，
 * 不回操作栏；分叉/钩子为 P1 占位钮）。
 */

/** 超过该字符数默认折叠为两行（R9 长用户消息；与原型 .clamp 行为一致——
 * 原型运行态演示消息约 130 字即出折叠钮，阈值取 120 覆盖同类长度）。 */
const CLAMP_THRESHOLD = 120;

function hhmmOf(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return '';
  const pad = (n: number): string => String(n).padStart(2, '0');
  return `${pad(at.getHours())}:${pad(at.getMinutes())}`;
}

function UserBubble({ item, canEdit }: { item: ChatMessageItem; canEdit: boolean }) {
  const [copied, setCopied] = useState(false);
  const [clamped, setClamped] = useState(true);
  const [editHint, setEditHint] = useState(false);
  const hintTimerRef = useRef<number | null>(null);
  const content = item.content ?? '';
  const clampable = content.length > CLAMP_THRESHOLD;

  useEffect(
    () => () => {
      if (hintTimerRef.current !== null) window.clearTimeout(hintTimerRef.current);
    },
    [],
  );

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

  // F1：编辑仅最新一条可用；编辑态无后端支撑（无消息编辑 API）——点击轻提示占位
  const onEdit = (): void => {
    setEditHint(true);
    if (hintTimerRef.current !== null) window.clearTimeout(hintTimerRef.current);
    hintTimerRef.current = window.setTimeout(() => setEditHint(false), 2500);
  };

  return (
    <div
      data-testid="message-user"
      data-message-id={item.id}
      className="msg-user"
    >
      <p className={clampable && clamped ? 'clamp' : undefined}>{content}</p>
      <div className="msg-acts">
        {clampable && (
          <button
            type="button"
            data-testid="message-clamp-toggle"
            className="chip-btn"
            onClick={() => setClamped((v) => !v)}
          >
            <Icon name="chevron-down" size={11} /> {clamped ? '展开' : '收起'}
          </button>
        )}
        <button type="button" data-testid="message-copy" className="chip-btn" onClick={copy}>
          <Icon name="copy" size={11} /> {copied ? '已复制' : '复制'}
        </button>
        {canEdit && (
          <button type="button" data-testid="message-edit" className="chip-btn" title="编辑（P1 占位）" onClick={onEdit}>
            <Icon name="pencil" size={11} /> 编辑
          </button>
        )}
        {editHint && (
          <span role="status" data-testid="message-edit-hint" className="open-lb self-center">
            编辑功能即将开放（P1）
          </span>
        )}
      </div>
    </div>
  );
}

function AssistantBubble({ item }: { item: ChatMessageItem }) {
  const finished = item.finishReason !== undefined;
  const finalText = useChatStore((s) => s.parts.get(partKeyOf(item.id, 'text'))?.text ?? '');
  const [copied, setCopied] = useState(false);
  const [vote, setVote] = useState<'up' | 'down' | null>(null);

  const copyFinal = (): void => {
    const clip = typeof navigator !== 'undefined' ? navigator.clipboard : undefined;
    if (clip !== undefined) {
      void clip.writeText(finalText).then(
        () => setCopied(true),
        () => setCopied(false),
      );
    } else {
      setCopied(true);
    }
  };

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
      {/* F4 完成态操作栏（R3§F 实测）：正常收口（stop/length/content_filter）时出现；
          赞/踩为本地反馈态，分叉/钩子为 P1 占位（钩子 = REQ-108 HookBridgeMiddleware） */}
      {finished && item.finishReason !== 'error' && item.finishReason !== 'cancelled' && (
        <div className="msg-foot" data-testid="message-foot">
          <button type="button" data-testid="message-foot-copy" className="chip-btn" onClick={copyFinal}>
            <Icon name="copy" size={11} /> {copied ? '已复制' : '复制'}
          </button>
          <button
            type="button"
            data-testid="message-thumb-up"
            className={`chip-btn ${vote === 'up' ? 'text-foreground-strong' : ''}`}
            aria-pressed={vote === 'up'}
            onClick={() => setVote((v) => (v === 'up' ? null : 'up'))}
          >
            <Icon name="thumbs-up" size={11} /> 赞
          </button>
          <button
            type="button"
            data-testid="message-thumb-down"
            className={`chip-btn ${vote === 'down' ? 'text-foreground-strong' : ''}`}
            aria-pressed={vote === 'down'}
            onClick={() => setVote((v) => (v === 'down' ? null : 'down'))}
          >
            <Icon name="thumbs-down" size={11} /> 踩
          </button>
          <button type="button" className="chip-btn" aria-disabled="true" title="分叉新任务（P1 占位）">
            <Icon name="fork" size={11} /> 分叉
          </button>
          <button type="button" className="chip-btn" aria-disabled="true" title="钩子（P1 HookBridgeMiddleware，REQ-108；P0 仅占位按钮）">
            <Icon name="hook" size={11} /> 钩子
          </button>
          <span className="tm">{hhmmOf(item.createdAt)}</span>
        </div>
      )}
    </div>
  );
}

export function MessageBubble({ item, isLatestUser = false }: { item: ChatMessageItem; isLatestUser?: boolean }) {
  return item.role === 'user' ? (
    <UserBubble item={item} canEdit={isLatestUser} />
  ) : (
    <AssistantBubble item={item} />
  );
}
