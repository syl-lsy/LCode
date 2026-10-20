/**
 * SseLineParser 单测（A4 §4.4；streaming-buffer 规则 4「半包」、§10.4 用例 1 的
 * 传输侧投影：同一字节流按任意 chunk 尺寸喂入，解析结果必须与一次性喂入完全一致）。
 *
 * 必测边界（testing.md 用例 1 清单）：多字节字符从中间切开、CRLF、跨 chunk 成形、
 * 一 chunk 多帧、多行 data、注释心跳、retry、流末残帧（不静默丢）。
 */
import { describe, expect, it } from 'vitest';

import messageDeltaFixture from '@contracts-fixtures/events/valid/message_delta.json';

import {
  chunkBytes,
  commentText,
  encodeText,
  frameText,
  retryText,
} from '@/tests/sse-fixtures';

import type { SseFrame, SseParserSignal } from './parser';
import { SseLineParser } from './parser';

type FrameSignal = Extract<SseParserSignal, { kind: 'frame' }>;

function framesOf(signals: readonly SseParserSignal[]): readonly SseFrame[] {
  return signals
    .filter((signal): signal is FrameSignal => signal.kind === 'frame')
    .map((signal) => signal.frame);
}

/** 按 size 字节切块喂入，收集全部信号（size=undefined 时一次性喂入）。 */
function feed(text: string, size?: number): SseParserSignal[] {
  const parser = new SseLineParser();
  const signals: SseParserSignal[] = [];
  const chunks = size === undefined ? [encodeText(text)] : chunkBytes(text, size);
  for (const chunk of chunks) signals.push(...parser.push(chunk));
  return signals;
}

/** 多语言载荷：CJK（3 字节）+ emoji（4 字节）+ 拉丁扩展（2 字节），专打字符边界。 */
const MULTIBYTE_DOC = [
  retryText(3000),
  frameText({ id: 1, event: 'message_delta', data: { delta: '中文 café 🚀 已定位' } }),
  commentText(),
  frameText({ id: 2, event: 'message_delta', data: { delta: '第二帧：éè🚀中' } }),
  frameText({ id: 3, event: 'run_status', data: 'a\nb' }), // 多行 data
].join('');

describe('SseLineParser：帧聚合', () => {
  it('完整三行帧一次喂入 → 单个 frame 信号（id/event/data 聚合）', () => {
    const signals = feed('id: 7\nevent: message_delta\ndata: {"seq":7}\n\n');
    expect(signals).toEqual([
      { kind: 'frame', frame: { id: '7', event: 'message_delta', data: '{"seq":7}' } },
    ]);
  });

  it('一 chunk 多帧：按到达序全部产出', () => {
    const text = [
      frameText({ id: 1, event: 'a', data: 'x' }),
      frameText({ id: 2, event: 'b', data: 'y' }),
      frameText({ id: 3, event: 'c', data: 'z' }),
    ].join('');
    const frames = framesOf(feed(text));
    expect(frames).toHaveLength(3);
    expect(frames.map((frame) => frame.id)).toEqual(['1', '2', '3']);
    expect(frames.map((frame) => frame.data)).toEqual(['x', 'y', 'z']);
  });

  it('半包：帧被切成任意字节块逐个喂入，结果与一次性喂入一致', () => {
    const text = frameText({ id: 11, event: 'message_delta', data: { delta: '半包测试🚀' } });
    const once = feed(text);
    for (const size of [1, 2, 3, 5, 7, 64]) {
      expect(feed(text, size), `chunk size=${size}`).toEqual(once);
    }
    expect(framesOf(once)[0]?.data).toBe(JSON.stringify({ delta: '半包测试🚀' }));
  });

  it('多字节字符跨 chunk 截断：逐字节喂入不产生乱码（streaming-buffer 规则 4）', () => {
    // chunkBytes(size=1) 必然把 3 字节 CJK / 4 字节 emoji 从中间切开
    const frames = framesOf(feed('data: 中文🚀é\n\n', 1));
    expect(frames).toHaveLength(1);
    expect(frames[0]?.data).toBe('中文🚀é');
  });

  it('前缀一致性（§10.4 用例 1 传输侧）：多种 chunk 尺寸下信号序列逐一深相等', () => {
    const once = feed(MULTIBYTE_DOC);
    expect(once).toEqual([
      { kind: 'retry', retryMs: 3000 },
      {
        kind: 'frame',
        frame: {
          id: '1',
          event: 'message_delta',
          data: JSON.stringify({ delta: '中文 café 🚀 已定位' }),
        },
      },
      { kind: 'heartbeat' },
      {
        kind: 'frame',
        frame: { id: '2', event: 'message_delta', data: JSON.stringify({ delta: '第二帧：éè🚀中' }) },
      },
      { kind: 'frame', frame: { id: '3', event: 'run_status', data: 'a\nb' } },
    ]);
    for (const size of [1, 2, 3, 5, 7, 13, 64, 4096]) {
      expect(feed(MULTIBYTE_DOC, size), `chunk size=${size}`).toEqual(once);
    }
  });

  it('CRLF 服务端：\\r\\n 行界与 \\n 等价（剥 \\r）', () => {
    const crlf = 'id: 5\r\nevent: run_status\r\ndata: {"ok":true}\r\n\r\n';
    const lf = 'id: 5\nevent: run_status\ndata: {"ok":true}\n\n';
    expect(framesOf(feed(crlf))).toEqual(framesOf(feed(lf)));
    // CRLF + 任意字节切块
    for (const size of [1, 3, 7]) {
      expect(framesOf(feed(crlf, size))).toEqual(framesOf(feed(lf)));
    }
  });

  it('多行 data：按 SSE 语义以 \\n 拼接', () => {
    const frames = framesOf(feed('data: line1\ndata: line2\ndata: line3\n\n'));
    expect(frames).toHaveLength(1);
    expect(frames[0]?.data).toBe('line1\nline2\nline3');
  });

  it('值的前导空格只剥一个（SSE 规范）', () => {
    expect(framesOf(feed('data: x\n\n'))[0]?.data).toBe('x');
    expect(framesOf(feed('data:  x\n\n'))[0]?.data).toBe(' x');
    expect(framesOf(feed('data:x\n\n'))[0]?.data).toBe('x');
  });

  it('无冒号字段行 = 字段名整行 + 空值；未知字段名忽略', () => {
    expect(framesOf(feed('data\n\n'))[0]?.data).toBe('');
    const frames = framesOf(feed('foo: bar\nretry-x: 1\ndata: kept\n\n'));
    expect(frames).toHaveLength(1);
    expect(frames[0]?.data).toBe('kept');
  });

  it('空帧不派发：连续空行、只有 id/event 的帧都产不出 frame', () => {
    expect(feed('\n\n\n')).toEqual([]);
    expect(feed('id: 9\n\n')).toEqual([]);
    expect(framesOf(feed('id: 9\nevent: x\n\ndata: real\n\n'))).toEqual([
      { id: undefined, event: undefined, data: 'real' },
    ]);
  });
});

describe('SseLineParser：注释心跳与 retry', () => {
  it('注释行 → heartbeat 信号，不产帧', () => {
    const signals = feed(commentText() + frameText({ id: 1, event: 'e', data: 'd' }));
    expect(signals[0]).toEqual({ kind: 'heartbeat' });
    expect(framesOf(signals)).toHaveLength(1);
  });

  it('retry: 纯数字 → retry 信号；非数字/空值忽略（SSE 规范）', () => {
    expect(feed(retryText(3000))).toEqual([{ kind: 'retry', retryMs: 3000 }]);
    expect(feed('retry: 5000\ndata: x\n\n')).toEqual([
      { kind: 'retry', retryMs: 5000 },
      { kind: 'frame', frame: { id: undefined, event: undefined, data: 'x' } },
    ]);
    expect(feed('retry: 3000ms\n')).toEqual([]);
    expect(feed('retry: abc\n')).toEqual([]);
    expect(feed('retry:\n')).toEqual([]);
  });
});

describe('SseLineParser：end() 冲刷与残帧', () => {
  it('干净结束（帧以空行收口）→ 不标记截断', () => {
    const parser = new SseLineParser();
    parser.push(encodeText(frameText({ id: 1, event: 'e', data: 'd' })));
    expect(parser.end()).toEqual({ truncated: false, partialLine: '', frameStarted: false });
  });

  it('残帧：行未换行终结（帧中间被截断）→ 显式标记，不派发不静默丢', () => {
    const parser = new SseLineParser();
    const signals = parser.push(encodeText('id: 5\nevent: message_delta\ndata: {"seq":5'));
    expect(signals).toEqual([]);
    expect(parser.end()).toEqual({
      truncated: true,
      partialLine: 'data: {"seq":5',
      frameStarted: true,
    });
  });

  it('残帧：行完整但缺帧分界空行 → 同样标记截断', () => {
    const parser = new SseLineParser();
    parser.push(encodeText('data: x\n'));
    expect(parser.end()).toEqual({ truncated: true, partialLine: '', frameStarted: true });
  });

  it('end() 冲刷 decoder 残尾：多字节字符只剩前半也不抛异常', () => {
    const parser = new SseLineParser();
    const bytes = encodeText('data: 🚀');
    parser.push(bytes.slice(0, bytes.length - 1)); // 砍掉 emoji 最后一个字节
    const result = parser.end();
    expect(result.truncated).toBe(true);
    expect(result.partialLine).toContain('\uFFFD'); // decoder flush 输出替换符而非崩溃
  });

  it('end() 后解析器归零：残帧不影响下一次生命周期（新实例语义）', () => {
    const parser = new SseLineParser();
    parser.push(encodeText('data: partial'));
    parser.end();
    expect(parser.end()).toEqual({ truncated: false, partialLine: '', frameStarted: false });
  });
});

describe('SseLineParser：契约同源冒烟', () => {
  it('真实契约 fixture 装帧后解析，data 可 JSON.parse 回原事件', () => {
    const text = frameText({
      id: messageDeltaFixture.seq,
      event: messageDeltaFixture.type,
      data: messageDeltaFixture,
    });
    const frames = framesOf(feed(text, 3)); // 顺带按 3 字节切块
    expect(frames).toHaveLength(1);
    expect(frames[0]?.id).toBe('118');
    expect(frames[0]?.event).toBe('message_delta');
    const parsed: unknown = JSON.parse(frames[0]?.data ?? 'null');
    expect(parsed).toEqual(messageDeltaFixture);
  });
});
