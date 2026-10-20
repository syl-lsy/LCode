/**
 * 通道缓冲单测（A4 §6.1 / §4.6；streaming-buffer 规则 10-13）。
 *
 * 覆盖：ChannelBuffer 定速（floor/k 与 60fps 标定一致）、batch 快速路径 appendImmediate
 * （不打字机、顺序保持）、64KB 上限降级整段渲染、终态 flushAll；TerminalBuffer 行缓冲
 * （半行不落库）、100ms 限频、待处理行上限即时冲刷；utf8ByteLength 多字节口径。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createFramePump, type FramePump } from './scheduler';
import {
  CHANNEL_PARAMS,
  ChannelBuffer,
  DeltaBufferRegistry,
  MAX_PENDING_BYTES,
  TerminalBuffer,
  TERMINAL_FLUSH_MS,
  utf8ByteLength,
} from './buffers';

interface ManualFrames {
  readonly pump: FramePump;
  run(): void;
}

function manualFrames(): ManualFrames {
  let sequence = 0;
  const pending = new Map<number, () => void>();
  const pump = createFramePump({
    requestFrame: (cb) => {
      const handle = ++sequence;
      pending.set(handle, cb);
      return handle;
    },
    cancelFrame: (handle) => {
      pending.delete(handle);
    },
    isHidden: () => false,
  });
  return {
    pump,
    run: () => {
      const callbacks = [...pending.values()];
      pending.clear();
      for (const cb of callbacks) cb();
    },
  };
}

interface SinkCall {
  readonly partKey: string;
  readonly text: string;
}

function createChannel(
  frames: ManualFrames,
  overrides: Partial<{ kind: 'text' | 'thinking'; maxPendingBytes: number }> = {},
): { buffer: ChannelBuffer; calls: SinkCall[] } {
  const calls: SinkCall[] = [];
  const buffer = new ChannelBuffer({
    partKey: 'msg-1:text',
    kind: overrides.kind ?? 'text',
    pump: frames.pump,
    sink: (partKey, text) => calls.push({ partKey, text }),
    ...(overrides.maxPendingBytes === undefined ? {} : { maxPendingBytes: overrides.maxPendingBytes }),
  });
  // 与生产同构：DeltaBufferRegistry 才负责把 drain 挂上泵；独立通道在测试里同样要挂，
  // 否则 pump.request() 只排帧、无回调可执行（缓冲永不消费）。
  frames.pump.add(() => buffer.drainFrame());
  return { buffer, calls };
}

afterEach(() => {
  vi.useRealTimers();
});

describe('ChannelBuffer：定速打字机', () => {
  it('每帧 drain max(floor, ceil(backlog × k))，正文 k=0.12 / 思考 k=0.25', () => {
    expect(CHANNEL_PARAMS.text).toEqual({ floor: 2, catchUp: 0.12 });
    expect(CHANNEL_PARAMS.thinking).toEqual({ floor: 2, catchUp: 0.25 });

    const frames = manualFrames();
    const { buffer, calls } = createChannel(frames);
    buffer.push('x'.repeat(100));

    frames.run();
    expect(calls).toHaveLength(1);
    expect(calls[0]?.text).toHaveLength(12); // ceil(100 × 0.12) = 12
    expect(buffer.pendingChars).toBe(88);

    frames.run();
    expect(calls[1]?.text).toHaveLength(11); // ceil(88 × 0.12) = 11
  });

  it('backlog 很小时用 floor（≥2 字符/帧，短 delta 也能推进）', () => {
    const frames = manualFrames();
    const { buffer, calls } = createChannel(frames);
    buffer.push('ab');

    frames.run();
    expect(calls[0]?.text).toBe('ab');
    expect(buffer.pendingChars).toBe(0);
  });

  it('思考通道追赶更快（同 backlog 每帧交出更多）', () => {
    const frames = manualFrames();
    const thinking = createChannel(frames, { kind: 'thinking' });
    thinking.buffer.push('y'.repeat(100));

    frames.run();
    expect(thinking.calls[0]?.text).toHaveLength(25); // ceil(100 × 0.25)
  });

  it('多帧才 drain 完的 backlog 顺序不变（拼接等于原文）', () => {
    const frames = manualFrames();
    const { buffer, calls } = createChannel(frames);
    const text = 'abcdefghijklmnopqrstuvwxyz0123456789';
    buffer.push(text);

    // 有界循环：定速参数正确时必然收敛；防回归（无限 drain）把测试套件挂死
    for (let frame = 0; frame < 1000 && buffer.pendingChars > 0; frame += 1) frames.run();
    expect(buffer.pendingChars).toBe(0);
    expect(calls.map((call) => call.text).join('')).toBe(text);
    expect(calls.length).toBeGreaterThan(1); // 确实分了多帧
  });

  it('64KB 上限：超限立即整段冲刷降级（不再打字机，§4.6）', () => {
    const frames = manualFrames();
    const { buffer, calls } = createChannel(frames, { maxPendingBytes: 8 });

    buffer.push('1234');
    expect(calls).toHaveLength(0); // 未超限：等帧
    buffer.push('56789'); // 累计 9 字节 > 8

    expect(calls).toEqual([{ partKey: 'msg-1:text', text: '123456789' }]);
    expect(buffer.pendingBytes).toBe(0);
    expect(buffer.pendingChars).toBe(0);
  });

  it('默认上限为 64KB', () => {
    expect(MAX_PENDING_BYTES).toBe(64 * 1024);
  });

  it('appendImmediate（batch 快速路径）：已排队内容合并为一次 sink，顺序保持', () => {
    const frames = manualFrames();
    const { buffer, calls } = createChannel(frames);
    buffer.push('流式');

    buffer.appendImmediate('重放');
    expect(calls).toEqual([{ partKey: 'msg-1:text', text: '流式重放' }]);
    expect(buffer.pendingChars).toBe(0);

    frames.run(); // 已无待渲染内容：不产生额外 sink 调用
    expect(calls).toHaveLength(1);
  });

  it('flushAll：终态冲刷剩余全部内容（已收内容不丢，规则 10）', () => {
    const frames = manualFrames();
    const { buffer, calls } = createChannel(frames);
    buffer.push('x'.repeat(97));

    buffer.flushAll();
    expect(calls).toEqual([{ partKey: 'msg-1:text', text: 'x'.repeat(97) }]);
    expect(buffer.pendingChars).toBe(0);

    buffer.flushAll(); // 幂等：无待渲染内容不再调用 sink
    expect(calls).toHaveLength(1);
  });
});

describe('DeltaBufferRegistry：多通道独立 + 一次注册 drain', () => {
  it('按 partKey 取/建通道，drainAll 每帧各 drain 一次，flushAll 全通道冲刷', () => {
    const frames = manualFrames();
    const textCalls: SinkCall[] = [];
    const thinkingCalls: SinkCall[] = [];
    // 注册表内建通道用默认 sink（写 chatStore）；此处用子类化不便，故直接断言通道实例行为
    const registry = new DeltaBufferRegistry(frames.pump);
    const textChannel = registry.channel('m1:text', 'text');
    const thinkingChannel = registry.channel('m1:thinking', 'thinking');

    expect(registry.size).toBe(2);
    expect(registry.channel('m1:text', 'text')).toBe(textChannel); // 同 key 复用

    registry.push('m1:text', 'text', 'x'.repeat(50));
    registry.push('m1:thinking', 'thinking', 'y'.repeat(50));
    expect(textChannel.pendingChars).toBe(50);
    expect(thinkingChannel.pendingChars).toBe(50);

    frames.run();
    expect(textChannel.pendingChars).toBe(50 - 6); // ceil(50 × 0.12) = 6
    expect(thinkingChannel.pendingChars).toBe(50 - 13); // ceil(50 × 0.25) = 13

    registry.flushAll();
    expect(textChannel.pendingChars).toBe(0);
    expect(thinkingChannel.pendingChars).toBe(0);

    registry.clear();
    expect(registry.size).toBe(0);
    expect(textCalls).toHaveLength(0);
    expect(thinkingCalls).toHaveLength(0);
  });
});

describe('TerminalBuffer：行缓冲 + 100ms 限频', () => {
  let frames: ManualFrames;
  let calls: { execId: string; lines: readonly string[] }[];
  let buffer: TerminalBuffer;

  beforeEach(() => {
    vi.useFakeTimers();
    frames = manualFrames();
    calls = [];
    buffer = new TerminalBuffer({
      sink: (execId, lines) => calls.push({ execId, lines }),
    });
  });

  it('完整行按行落库，未换行尾段保留在缓冲内（半行不拆成多行）', () => {
    buffer.append('call_1', 'line1\nline2\npartial');

    vi.advanceTimersByTime(TERMINAL_FLUSH_MS);
    expect(calls).toEqual([{ execId: 'call_1', lines: ['line1', 'line2'] }]);

    // 尾段在后续数据到达后成行
    buffer.append('call_1', '-rest\nnext\n');
    vi.advanceTimersByTime(TERMINAL_FLUSH_MS);
    expect(calls).toEqual([
      { execId: 'call_1', lines: ['line1', 'line2'] },
      { execId: 'call_1', lines: ['partial-rest', 'next'] },
    ]);
  });

  it('100ms 内多次 append 合并为一次落库（限频，§6.3）', () => {
    buffer.append('call_1', 'a\n');
    vi.advanceTimersByTime(TERMINAL_FLUSH_MS / 2);
    buffer.append('call_1', 'b\n');
    expect(calls).toHaveLength(0);

    vi.advanceTimersByTime(TERMINAL_FLUSH_MS / 2);
    expect(calls).toEqual([{ execId: 'call_1', lines: ['a', 'b'] }]);
  });

  it('flush() 立即落库（含尾段），run 终态时用', () => {
    buffer.append('call_1', 'tail-without-newline');
    buffer.append('call_2', 'other\n');

    buffer.flush();
    expect(calls).toEqual([
      { execId: 'call_1', lines: ['tail-without-newline'] },
      { execId: 'call_2', lines: ['other'] },
    ]);
    expect(buffer.pendingLines).toBe(0);
  });

  it('待处理行超过上限 → 立即冲刷（防单次大批量撑爆内存）', () => {
    buffer = new TerminalBuffer({
      sink: (execId, lines) => calls.push({ execId, lines }),
      maxPendingLines: 3,
    });
    buffer.append('call_1', '1\n2\n3\n4\n');

    expect(calls).toEqual([{ execId: 'call_1', lines: ['1', '2', '3', '4'] }]);
    expect(buffer.pendingLines).toBe(0);
  });

  it('clear() 丢弃待处理内容并停掉定时器（测试隔离）', () => {
    buffer.append('call_1', 'pending\n');
    buffer.clear();

    vi.advanceTimersByTime(TERMINAL_FLUSH_MS * 2);
    expect(calls).toHaveLength(0);
    expect(frames.pump.pending).toBe(false);
  });
});

describe('utf8ByteLength：64KB 口径按字节而非字符', () => {
  it('ASCII / 2 字节 / 3 字节（CJK）/ 4 字节（emoji）分别计 1/2/3/4', () => {
    expect(utf8ByteLength('abc')).toBe(3);
    expect(utf8ByteLength('é')).toBe(2);
    expect(utf8ByteLength('中')).toBe(3);
    expect(utf8ByteLength('🚀')).toBe(4);
    expect(utf8ByteLength('a中🚀')).toBe(8);
    expect(utf8ByteLength('')).toBe(0);
  });

  it('孤立高位代理（后随非低位代理）按 3 字节计（防御非法序列）', () => {
    expect(utf8ByteLength('\ud800x')).toBe(4); // 3（替换孤立代理）+ 1
  });
});

describe('空输入防御：push/appendImmediate/append 的空串都是 no-op', () => {
  it('ChannelBuffer.push("") 不入队不排帧', () => {
    const frames = manualFrames();
    const { buffer } = createChannel(frames);

    buffer.push('');
    expect(buffer.pendingChars).toBe(0);
    expect(frames.pump.pending).toBe(false);
  });

  it('ChannelBuffer.appendImmediate("") 不触发 sink', () => {
    const frames = manualFrames();
    const { buffer, calls } = createChannel(frames);

    buffer.appendImmediate('');
    expect(calls).toEqual([]);
  });

  it('TerminalBuffer.append("") 不建 entry 不排定时器', () => {
    vi.useFakeTimers();
    const calls: { execId: string; lines: readonly string[] }[] = [];
    const buffer = new TerminalBuffer({ sink: (execId, lines) => calls.push({ execId, lines }) });

    buffer.append('call_1', '');
    vi.advanceTimersByTime(TERMINAL_FLUSH_MS * 2);
    expect(calls).toEqual([]);
    expect(buffer.pendingLines).toBe(0);
  });
});
