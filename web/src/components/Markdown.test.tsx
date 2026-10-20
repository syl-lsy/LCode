// @vitest-environment jsdom
/**
 * 流式用例 1 · markdown 前缀一致性（A4 §10.4 用例 1；streaming-buffer 规则 15）：
 *
 * 完整文档按 1/7/64/随机字符切块逐段喂入（模拟 message_delta 累积），**每个前缀**
 * 的流式渲染结果 == 该前缀一次性渲染结果；必含多字节字符截断（CJK/emoji 被切在
 * 代理对中间）、CRLF 行尾、跨 chunk 表格、未闭合围栏 → 纯文本、闭合后升级高亮。
 */
import { act, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import '../tests/test-utils';
import { Markdown, MARKDOWN_PARSE_INTERVAL_MS, hasUnclosedFence, useThrottledText } from './Markdown';

/** 覆盖多字节/CRLF/表格/围栏/列表的文档（CRLF 行尾） */
const DOC_LINES = [
  '# 回放说明',
  '',
  '多字节：中文片段、emoji 🐍 与全角（含）标点。',
  '',
  '| 文件 | 增 | 删 |',
  '| --- | --- | --- |',
  '| hello.py | 12 | 3 |',
  '',
  '```python',
  'def hello(name):',
  '    print(f"你好，{name}")',
  '```',
  '',
  '- 列表项 1',
  '- 列表项 2',
  '',
  '> 引用块结尾 🙈',
];
const DOC = DOC_LINES.join('\r\n');

/** 一次性渲染取 innerHTML（每个前缀的对照基准）。 */
function renderOneShot(text: string): string {
  const container = document.createElement('div');
  document.body.append(container);
  const view = render(<Markdown text={text} />, { container });
  const html = container.innerHTML;
  view.unmount();
  container.remove();
  return html;
}

/** 按 chunkSize 切块（按 UTF-16 码元切——必然把代理对/多字节字符切开）。 */
function chunkBy(text: string, chunkSize: number): string[] {
  const chunks: string[] = [];
  for (let i = 0; i < text.length; i += chunkSize) chunks.push(text.slice(i, i + chunkSize));
  return chunks;
}

function randomChunks(text: string, seed: number): string[] {
  let state = seed;
  const next = (): number => {
    state = (state * 1103515245 + 12345) % 2147483648;
    return state / 2147483648;
  };
  const chunks: string[] = [];
  let cursor = 0;
  while (cursor < text.length) {
    const size = 1 + Math.floor(next() * 9);
    chunks.push(text.slice(cursor, cursor + size));
    cursor += size;
  }
  return chunks;
}

/** 逐块喂入：每个前缀的流式 DOM == 该前缀一次性渲染 DOM。 */
function assertPrefixConsistency(chunks: string[]): void {
  const view = render(<Markdown text={chunks[0] ?? ''} />);
  let prefix = chunks[0] ?? '';
  expect(view.container.innerHTML).toBe(renderOneShot(prefix));
  for (const chunk of chunks.slice(1)) {
    prefix += chunk;
    view.rerender(<Markdown text={prefix} />);
    act(() => {
      vi.advanceTimersByTime(MARKDOWN_PARSE_INTERVAL_MS + 10); // 越过限频窗口
    });
    expect(view.container.innerHTML).toBe(renderOneShot(prefix));
  }
  view.unmount();
}

describe('Markdown · 流式用例 1：前缀一致性', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it.each([1, 7, 64])('按 %i 码元切块：每个前缀渲染 == 该前缀一次性渲染', (size) => {
    vi.useFakeTimers();
    assertPrefixConsistency(chunkBy(DOC, size));
    expect(vi.getTimerCount()).toBe(0);
  });

  it('随机切块（2 个种子）：每个前缀渲染 == 该前缀一次性渲染', () => {
    vi.useFakeTimers();
    assertPrefixConsistency(randomChunks(DOC, 42));
    assertPrefixConsistency(randomChunks(DOC, 20261018));
  });

  it('多字节截断：CJK/emoji 被切在代理对中间不崩，终态与一次性渲染一致', () => {
    vi.useFakeTimers();
    const doc = '前缀 🙈🐍 中文收尾';
    const chunks = chunkBy(doc, 1);
    expect(chunks.some((c) => c.length === 1 && c.charCodeAt(0) >= 0xd800 && c.charCodeAt(0) < 0xdc00)).toBe(true);
    assertPrefixConsistency(chunks);
    const view = render(<Markdown text={doc} />);
    expect(view.container.innerHTML).toBe(renderOneShot(doc));
    expect(view.container.textContent).toContain('🙈🐍');
  });

  it('未闭合围栏 → 纯文本；闭合后升级为高亮 markdown（跨 chunk 表格同时成立）', () => {
    vi.useFakeTimers();
    const partial = '回复开始\r\n\r\n```bash\r\necho hello';
    expect(hasUnclosedFence(partial)).toBe(true);
    const plain = render(<Markdown text={partial} />);
    expect(plain.getByTestId('markdown-plain')).toBeInTheDocument();
    expect(plain.getByTestId('markdown-plain')).toHaveTextContent('```bash');
    plain.unmount();

    const closed = [partial, '```', '', '| A | B |', '| --- | --- |', '| 1 | 2 |'].join('\r\n');
    expect(hasUnclosedFence(closed)).toBe(false);
    const view = render(<Markdown text={closed} />);
    expect(view.getByTestId('markdown-body')).toBeInTheDocument();
    expect(view.container.querySelector('.hljs')).not.toBeNull();
    expect(view.container.querySelector('table')).not.toBeNull();
  });

  it('70ms 限频：text 变化不立即重解析，窗口到期后提交（限频只延迟不改变结果）', () => {
    vi.useFakeTimers();
    const view = render(<Markdown text="第一版" />);
    expect(view.container.textContent).toContain('第一版');

    view.rerender(<Markdown text={'第二版 ' + 'x'.repeat(20)} />);
    // 未越窗：仍是第一版（防每 delta 全量重解析）
    expect(view.container.textContent).not.toContain('第二版');
    act(() => {
      vi.advanceTimersByTime(MARKDOWN_PARSE_INTERVAL_MS + 5);
    });
    expect(view.container.textContent).toContain('第二版');
  });

  it('useThrottledText：初始值同步可见（首屏无闪烁），后续变化越窗提交', () => {
    vi.useFakeTimers();
    function Probe({ text }: { text: string }) {
      const rendered = useThrottledText(text);
      return <span data-testid="probe">{rendered}</span>;
    }
    const view = render(<Probe text="A" />);
    expect(view.getByTestId('probe')).toHaveTextContent('A');
    view.rerender(<Probe text="B" />);
    act(() => {
      vi.advanceTimersByTime(MARKDOWN_PARSE_INTERVAL_MS + 5);
    });
    expect(view.getByTestId('probe')).toHaveTextContent('B');
  });
});
