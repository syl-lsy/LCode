import { useId, useState } from 'react';

import type { ChatToolCardItem, ToolErrorPayload } from '../store';

/**
 * 工具卡片（A4 §8.1 ToolCard 行；R15 三卡型 + AC-003-2 两态）。
 *
 * 一行摘要（图标 + 类型标签 + 命令/路径预览）+ 展开详情（入参/出参/错误）；
 * 状态两态：running（spinner）/ 收口（success 打勾 | error 标点）；完成态保留折叠行
 * （R9#13）。exit_code 语义：非 0 不代表 status=error（deepagents 口径），单独出
 * 「exit N」徽标（AC-002-1 退出码经 tool_call_finished 补全）。
 */

/** 工具名 → 卡型标签（R15：查阅/终端/写入；其余按工具名原样）。 */
function cardKindOf(tool: string): { label: string; icon: string } {
  if (tool === 'execute') return { label: '终端', icon: '❯_' };
  if (tool === 'read_file' || tool === 'ls' || tool === 'glob' || tool === 'grep') {
    return { label: '查阅', icon: '⌕' };
  }
  if (tool === 'write_file' || tool === 'edit_file') return { label: '写入', icon: '✎' };
  return { label: tool, icon: '⚙' };
}

/** 摘要预览：execute → command；read/write → path；其余取首个标量参数。 */
function summaryOf(item: ChatToolCardItem): string {
  const args = item.args as Record<string, unknown>;
  const command = args['command'];
  if (typeof command === 'string') return command;
  for (const key of ['path', 'file_path', 'file', 'query', 'pattern', 'url']) {
    const value = args[key];
    if (typeof value === 'string') return value;
  }
  const first = Object.values(args).find((v) => typeof v === 'string' || typeof v === 'number');
  return first === undefined ? '' : String(first);
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

export function ToolCard({ item }: { item: ChatToolCardItem }) {
  const [expanded, setExpanded] = useState(false);
  const detailId = useId();
  const kind = cardKindOf(item.tool);
  const summary = summaryOf(item);
  const running = item.status === 'running';
  const failed = item.status === 'error';

  return (
    <div data-testid="tool-card" data-status={item.status} className="mb-2">
      <button
        type="button"
        aria-expanded={expanded}
        aria-controls={detailId}
        data-testid="tool-card-toggle"
        className="flex w-full items-center gap-2 rounded-md border border-border bg-card px-3 py-2 text-left transition-colors hover:border-border-hover"
        onClick={() => setExpanded((v) => !v)}
      >
        <span
          aria-hidden="true"
          className="flex h-6 w-6 flex-none items-center justify-center rounded-sm bg-surface font-mono text-[11px] text-foreground-subtle"
        >
          {kind.icon}
        </span>
        <span className="flex-none text-[length:var(--text-ui-xs)] text-foreground-subtle">
          {running ? `正在${kind.label}` : kind.label}
        </span>
        <code className="min-w-0 flex-1 truncate font-mono text-[length:var(--code-font-size)] text-foreground">
          {summary}
        </code>
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
        {running && <span className="spinner flex-none" aria-hidden="true" />}
        {!running && !failed && (
          <span aria-hidden="true" className="flex-none text-success">
            ✓
          </span>
        )}
        {failed && item.error !== null && item.error !== undefined && (
          <ErrorCodeBadge error={item.error} />
        )}
        <span aria-hidden="true" className="flex-none text-foreground-subtlest">
          {expanded ? '▾' : '▸'}
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
    </div>
  );
}
