import { memo, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';

import './markdown.css';

/**
 * 限频增量 markdown 渲染器（A4 §6.2，全站唯一；streaming-buffer 规则 15）。
 *
 * - 选型：react-markdown + remark-gfm + rehype-highlight（成熟稳定、流式场景广泛
 *   验证；rehype-highlight 默认 = highlight.js common 语言子集，含 bash/python/ts/
 *   js/json/diff，控包体）；
 * - M2 基础层：限频重解析——`text` 变化只置脏，每 70ms 真正 parse 一次；卸载/
 *   text 稳定后冲刷末次（终态内容不丢）；
 * - 未闭合代码围栏（围栏记数为奇）按纯文本渲染，闭合后升级高亮（规则 15）；
 * - 前缀一致性（§10.4 用例 1）：本组件是 text 的纯函数——同一文本渲染结果确定，
 *   限频只延迟不改变结果，任意切块累积的前缀渲染 == 该前缀一次性渲染。
 *
 * memo 化（§6.2）：text 引用仅在 drain 后变化，父级高频重渲不触发重解析。
 */

/** 限频窗口（ms，§6.2）。 */
export const MARKDOWN_PARSE_INTERVAL_MS = 70;

/**
 * 未闭合围栏检测：统计行首围栏标记（``` 或 ~~~，≤3 空格缩进）个数，奇数 = 有
 * 围栏未闭合 → 按纯文本渲染。CRLF（\r\n）行尾不影响匹配。
 */
export function hasUnclosedFence(text: string): boolean {
  let count = 0;
  for (const rawLine of text.split('\n')) {
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine;
    if (/^\s{0,3}(```|~~~)/.test(line)) count += 1;
  }
  return count % 2 === 1;
}

/** 限频消费：text 高频变化时每 70ms 才提交一次渲染值；卸载时冲刷最后值。 */
export function useThrottledText(text: string, intervalMs = MARKDOWN_PARSE_INTERVAL_MS): string {
  const [rendered, setRendered] = useState(text);
  const lastFlushAtRef = useRef(0);
  const timerRef = useRef<number | null>(null);
  const pendingRef = useRef<string | null>(null);

  useEffect(() => {
    pendingRef.current = text;
    const flush = (): void => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      if (pendingRef.current === null) return;
      const value = pendingRef.current;
      pendingRef.current = null;
      lastFlushAtRef.current = Date.now();
      setRendered(value);
    };

    const elapsed = Date.now() - lastFlushAtRef.current;
    if (elapsed >= intervalMs) {
      flush();
      return () => undefined;
    }
    timerRef.current = window.setTimeout(flush, intervalMs - elapsed);
    return () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [text, intervalMs]);

  return rendered;
}

export interface MarkdownProps {
  text: string;
}

function MarkdownBody({ text }: MarkdownProps) {
  const rendered = useThrottledText(text);
  if (hasUnclosedFence(rendered)) {
    // 未闭合围栏：纯文本渲染（不喂 markdown 解析器），闭合后升级高亮（规则 15）
    return (
      <pre
        data-testid="markdown-plain"
        className="overflow-x-auto rounded-md border border-border bg-terminal-bg px-3 py-2 font-mono text-[length:var(--code-font-size)] leading-[1.7] whitespace-pre-wrap break-words text-terminal-fg"
      >
        {rendered}
      </pre>
    );
  }
  return (
    <div data-testid="markdown-body" className="md-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          a: ({ children, href }) => (
            <a href={href} target="_blank" rel="noreferrer noopener">
              {children}
            </a>
          ),
        }}
      >
        {rendered}
      </ReactMarkdown>
    </div>
  );
}

/** 全站唯一 markdown 渲染入口（流式正文 / 思考 / 静态文本共用）。 */
export const Markdown = memo(MarkdownBody);
