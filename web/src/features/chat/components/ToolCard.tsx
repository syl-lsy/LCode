import { useId, useState } from 'react';

import type { ChatToolCardItem, ToolErrorPayload } from '../store';

import { Icon, type IconName } from '@/components/Icon';

/**
 * 工具卡片（A4 §8.1 ToolCard 行；R15 三卡型 + AC-003-2 两态；来源
 * design/prototype/task-conversation.html .tool-card）。
 *
 * 一行摘要（图标 + 类型标签 + 命令/路径预览）+ 展开详情（入参/出参/错误）；三卡型
 * 差异化（R15）：终端（terminal 图标）/ 查阅（search 图标 + 对象计数徽标）/ 写入
 * （pencil 图标 + 新增行数徽标 + 展开渲染内容代码块——新文件全为新增行，.cl.add）；
 * task 工具用 user 图标（子智能体卡，扩展区块由 SubagentCard 挂载）。
 * 状态两态：running（「正在{类型}」前缀）/ 收口（success 无标 | error 标点）；
 * exit_code 语义：非 0 不代表 status=error（deepagents 口径），单独出「exit N」徽标。
 */

type CardFamily = 'execute' | 'read' | 'write' | 'task' | 'other';

interface CardKind {
  readonly label: string;
  readonly icon: IconName;
  readonly family: CardFamily;
}

/** 工具名 → 卡型（R15：查阅/终端/写入；其余按工具名原样）。 */
function cardKindOf(tool: string): CardKind {
  if (tool === 'execute') return { label: '终端', icon: 'terminal', family: 'execute' };
  if (tool === 'read_file' || tool === 'ls' || tool === 'glob' || tool === 'grep') {
    return { label: '查阅', icon: 'search', family: 'read' };
  }
  if (tool === 'write_file' || tool === 'edit_file') {
    return { label: '写入', icon: 'pencil', family: 'write' };
  }
  if (tool === 'task') return { label: '子智能体', icon: 'user', family: 'task' };
  return { label: tool, icon: 'settings', family: 'other' };
}

/** 摘要预览：execute → command；read/write → path；其余取首个标量参数。 */
function summaryOf(item: ChatToolCardItem): string {
  const args = item.args as Record<string, unknown>;
  const command = args['command'];
  if (typeof command === 'string') return command;
  for (const key of ['path', 'file_path', 'file', 'query', 'pattern', 'url', 'description']) {
    const value = args[key];
    if (typeof value === 'string') return value;
  }
  const first = Object.values(args).find((v) => typeof v === 'string' || typeof v === 'number');
  return first === undefined ? '' : String(first);
}

/** 写入类内容探针（write_file.content / edit_file.new_string 等约定键）。 */
const WRITE_CONTENT_KEYS = ['content', 'new_string', 'new_text', 'code'] as const;

function writeContentOf(item: ChatToolCardItem): string | null {
  const args = item.args as Record<string, unknown>;
  for (const key of WRITE_CONTENT_KEYS) {
    const value = args[key];
    if (typeof value === 'string') return value;
  }
  return null;
}

/** 文件扩展名 → 代码块语言标签（无扩展名回退 'code'）。 */
function langOf(path: string): string {
  const dot = path.lastIndexOf('.');
  if (dot < 0 || dot === path.length - 1) return 'code';
  return path.slice(dot + 1).toLowerCase();
}

function ErrorCodeBadge({ error }: { error: ToolErrorPayload }) {
  return (
    <span
      data-testid="tool-error-code"
      className="badge flex-none border-destructive/40 text-destructive"
    >
      {error.code}
    </span>
  );
}

/** 写入类展开代码块（R8 代码块组件 .codeblk）：新文件内容全为新增行（.cl.add）。 */
function WriteCodeBlock({ path, content }: { path: string; content: string }) {
  const [wrap, setWrap] = useState(false);
  const [copied, setCopied] = useState(false);
  const lines = content.split('\n');

  const copy = (): void => {
    const clip = typeof navigator !== 'undefined' ? navigator.clipboard : undefined;
    if (clip !== undefined) {
      void clip.writeText(content).then(
        () => setCopied(true),
        () => setCopied(false),
      );
    } else {
      setCopied(true);
    }
  };

  return (
    <div className={`codeblk ${wrap ? 'wrap-on' : ''}`} data-testid="tool-codeblk" title={path}>
      <div className="cb-head">
        <span className="cb-lang">{langOf(path)}</span>
        <span className="grow" aria-hidden="true" style={{ flex: 1 }} />
        <button
          type="button"
          className="cb-btn"
          aria-pressed={wrap}
          onClick={() => setWrap((v) => !v)}
        >
          <Icon name="wrap" size={10} /> 自动换行
        </button>
        <button type="button" className="cb-btn" data-testid="codeblk-copy" onClick={copy}>
          <Icon name="copy" size={10} /> {copied ? '已复制' : '复制代码'}
        </button>
      </div>
      <div className="cb-body scroll-thin">
        {lines.map((line, idx) => (
          <div key={idx} className="cl add">
            <span className="no">{idx + 1}</span>
            <span className="ct">{line === '' ? ' ' : line}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function ToolCard({ item }: { item: ChatToolCardItem }) {
  const [expanded, setExpanded] = useState(false);
  const detailId = useId();
  const kind = cardKindOf(item.tool);
  const summary = summaryOf(item);
  const running = item.status === 'running';
  const failed = item.status === 'error';

  // 写入卡：新增行数徽标（+N = 入参内容行数；F15 口径——来自入参事实，非 diff 事实）
  const content = kind.family === 'write' ? writeContentOf(item) : null;
  const addLines = content !== null ? content.split('\n').length : 0;
  const writePath =
    kind.family === 'write'
      ? (() => {
          const args = item.args as Record<string, unknown>;
          for (const key of ['path', 'file_path', 'file']) {
            const value = args[key];
            if (typeof value === 'string') return value;
          }
          return '';
        })()
      : '';
  // 查阅卡：出参对象计数徽标（·N 行 = result_preview 行数，如实计数）
  const resultLines =
    !running && kind.family === 'read' && item.resultPreview !== null && item.resultPreview !== undefined && item.resultPreview !== ''
      ? item.resultPreview.split('\n').length
      : 0;

  return (
    <div data-testid="tool-card" data-status={item.status} className="mb-2">
      <button
        type="button"
        aria-expanded={expanded}
        aria-controls={detailId}
        data-testid="tool-card-toggle"
        className="tool-card"
        title="展开工具详情"
        onClick={() => setExpanded((v) => !v)}
      >
        <span className="tic" aria-hidden="true">
          <Icon name={kind.icon} size={12} />
        </span>
        <span className="lb">{running ? `正在${kind.label}` : kind.label}</span>
        <code>{summary}</code>
        {kind.family === 'write' && addLines > 0 && (
          <span data-testid="tool-add-badge" className="badge badge-add flex-none">
            +{addLines}
          </span>
        )}
        {resultLines > 0 && (
          <span data-testid="tool-obj-badge" className="obj-badge">
            · {resultLines} 行
          </span>
        )}
        {item.exitCode !== null && item.exitCode !== undefined && item.exitCode !== 0 && (
          <span
            data-testid="tool-exit-code"
            className="badge flex-none border-warning/40 text-warning"
          >
            exit {item.exitCode}
          </span>
        )}
        {item.truncated === true && (
          <span className="badge flex-none" title="结果被截断，完整引用见 result_ref">
            已截断
          </span>
        )}
        {failed && item.error !== null && item.error !== undefined && (
          <ErrorCodeBadge error={item.error} />
        )}
        <span className="chev" aria-hidden="true">
          <Icon name={expanded ? 'chevron-up' : 'chevron-down'} size={11} />
        </span>
      </button>

      {expanded && (
        <div
          id={detailId}
          data-testid="tool-card-detail"
          className="rounded-b-md border border-t-0 border-border bg-surface px-3 py-2 font-mono text-[length:var(--code-font-size)] text-foreground-subtle"
        >
          <div>
            <span className="text-foreground-subtlest">入参 </span>
            <span className="break-all">{JSON.stringify(item.args)}</span>
          </div>
          {item.resultPreview !== null && item.resultPreview !== undefined && item.resultPreview !== '' && (
            <div className="mt-1">
              <span className="text-foreground-subtlest">出参 </span>
              <span className="whitespace-pre-wrap break-all">{item.resultPreview}</span>
              {item.resultRef !== null && item.resultRef !== undefined && item.resultRef !== '' && (
                <div className="mt-0.5 text-foreground-subtlest">完整结果：{item.resultRef}</div>
              )}
            </div>
          )}
          {failed && item.error !== null && item.error !== undefined && (
            <div className="mt-1 text-destructive" data-testid="tool-error-detail">
              {item.error.code}: {item.error.message}
              {item.error.code === 'sandbox_unavailable' && (
                <span className="mt-0.5 block text-warning" data-testid="tool-error-sandbox">
                  沙箱不可用，运行已尝试重建或转报（AC-002-4）
                </span>
              )}
            </div>
          )}
          {item.durationMs !== undefined && (
            <div className="mt-1 text-foreground-subtlest">耗时 {item.durationMs} ms</div>
          )}
        </div>
      )}
      {expanded && content !== null && <WriteCodeBlock path={writePath} content={content} />}
    </div>
  );
}
