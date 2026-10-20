/**
 * SessionEventStream / StreamManager 集成测试（A4 §4 全节 + §10.1「注入 fake
 * transport」；streaming-buffer 规则 1/3/5/24，§10.4 用例 2 的传输侧投影）。
 *
 * 手法：假 transport（Deferred<Response> + 可控 ReadableStream，见
 * src/tests/sse-fixtures.ts）+ vitest 假定时器驱动退避/心跳/超时。
 * 覆盖：三条连接路径（A 无水位首连 / B ensureCaughtUp / C 断线重连带
 * Last-Event-ID）、状态机迁移、退避序列与重置、心跳超时、流前 REST 错误体终止、
 * close 幂等、seq 跟踪（单调不回退）、未知事件类型透传、迟到响应 stale 守卫。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import messageDeltaFixture from '@contracts-fixtures/events/valid/message_delta.json';

import {
  commentText,
  createControllableStream,
  createDeferred,
  eventStreamResponse,
  frameText,
  pendingUntilAbort,
  readHeader,
  restErrorResponse,
  retryText,
  type ControllableStream,
  type Deferred,
  type RecordedCall,
} from '@/tests/sse-fixtures';

import type { SseFrame } from './parser';
import type { BackoffConfig, ConnState, Transport } from './sse';
import { SessionEventStream, StreamManager, streamManager } from './sse';

const EVENTS_URL = '/api/v1/sessions/sess-1/events';

interface Attempt {
  readonly call: RecordedCall;
  control?: ControllableStream;
  deferred?: Deferred<Response>;
}

type Responder = (attempt: Attempt) => Promise<Response>;

interface HarnessOptions {
  readonly sessionId?: string;
  readonly responder?: Responder;
  readonly heartbeatTimeoutMs?: number;
  readonly connectTimeoutMs?: number;
  readonly backoff?: Partial<BackoffConfig>;
  readonly onFrame?: (frame: SseFrame) => void;
}

interface Harness {
  readonly stream: SessionEventStream;
  readonly attempts: Attempt[];
  readonly frames: SseFrame[];
  readonly states: ConnState[];
  readonly heartbeats: { count: number };
  settle(): Promise<void>;
  advance(ms: number): Promise<void>;
  open(i: number): Promise<void>;
  resolveRaw(i: number, response: Response): Promise<void>;
  resolveRestError(i: number, status: number, code: string, message?: string): Promise<void>;
  push(i: number, text: string): void;
  url(i: number): string;
  header(i: number, name: string): string | undefined;
  signalOf(i: number): AbortSignal | null | undefined;
  callCount(): number;
  lastState(): ConnState | undefined;
}

function createHarness(options: HarnessOptions = {}): Harness {
  const attempts: Attempt[] = [];
  const frames: SseFrame[] = [];
  const states: ConnState[] = [];
  const heartbeats = { count: 0 };

  const defaultResponder: Responder = (attempt) => {
    const control = createControllableStream(attempt.call.init.signal);
    const deferred = createDeferred<Response>();
    attempt.control = control;
    attempt.deferred = deferred;
    return deferred.promise;
  };
  const responder = options.responder ?? defaultResponder;

  const transport: Transport = (url, init) => {
    const attempt: Attempt = { call: { url, init } };
    attempts.push(attempt);
    return responder(attempt);
  };

  const stream = new SessionEventStream({
    sessionId: options.sessionId ?? 'sess-1',
    transport,
    onFrame: options.onFrame ?? ((frame) => frames.push(frame)),
    onState: (state) => states.push(state),
    onHeartbeat: () => {
      heartbeats.count += 1;
    },
    heartbeatTimeoutMs: options.heartbeatTimeoutMs,
    connectTimeoutMs: options.connectTimeoutMs,
    backoff: options.backoff,
  });

  const at = (i: number): Attempt => {
    const attempt = attempts[i];
    if (!attempt) throw new Error(`attempt[${i}] 不存在（当前共 ${attempts.length} 次调用）`);
    return attempt;
  };
  const controlAt = (i: number): ControllableStream => {
    const control = at(i).control;
    if (!control) throw new Error(`attempt[${i}] 非可控流（自定义 responder?）`);
    return control;
  };
  const deferredAt = (i: number): Deferred<Response> => {
    const deferred = at(i).deferred;
    if (!deferred) throw new Error(`attempt[${i}] 无 deferred Response`);
    return deferred;
  };
  const settle = async (): Promise<void> => {
    // 三次 0ms 推进：冲刷 transport→headers→body 链路上的连续 microtask
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(0);
  };

  return {
    stream,
    attempts,
    frames,
    states,
    heartbeats,
    settle,
    advance: async (ms) => {
      await vi.advanceTimersByTimeAsync(ms);
    },
    open: async (i) => {
      deferredAt(i).resolve(eventStreamResponse(controlAt(i).stream));
      await settle();
    },
    resolveRaw: async (i, response) => {
      deferredAt(i).resolve(response);
      await settle();
    },
    resolveRestError: async (i, status, code, message) => {
      deferredAt(i).resolve(restErrorResponse(status, code, message));
      await settle();
    },
    push: (i, text) => controlAt(i).push(text),
    url: (i) => at(i).call.url,
    header: (i, name) => readHeader(at(i).call.init, name),
    signalOf: (i) => at(i).call.init.signal,
    callCount: () => attempts.length,
    lastState: () => states[states.length - 1],
  };
}

function warnText(): string {
  return vi
    .mocked(console.warn)
    .mock.calls.map((call) => call.join(' '))
    .join('\n');
}

function errorText(): string {
  return vi
    .mocked(console.error)
    .mock.calls.map((call) => call.join(' '))
    .join('\n');
}

/** manager 测试用的双次 microtask 冲刷（无 harness.settle 可用）。 */
async function flush(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
  await vi.advanceTimersByTimeAsync(0);
  await vi.advanceTimersByTimeAsync(0);
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(console, 'warn').mockImplementation(() => undefined);
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('路径 A：打开/刷新会话（无水位首连全量重放）', () => {
  it('首连 URL 无 after_seq、无 Last-Event-ID；200+event-stream → connecting→open', async () => {
    const h = createHarness();
    const connected = h.stream.connect();
    await h.settle();
    expect(h.callCount()).toBe(1);
    expect(h.url(0)).toBe(EVENTS_URL);
    expect(h.header(0, 'Last-Event-ID')).toBeUndefined();
    expect(h.header(0, 'Accept')).toBe('text/event-stream');
    expect(h.lastState()).toBe('connecting');

    await h.open(0);
    expect(h.states).toEqual(['connecting', 'open']);
    await expect(connected).resolves.toBeUndefined(); // open 即首次 settle
  });

  it('契约 fixture 帧到达 → onFrame 透传、lastSeq 更新为帧 id', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.open(0);
    h.push(
      0,
      frameText({
        id: messageDeltaFixture.seq,
        event: messageDeltaFixture.type,
        data: messageDeltaFixture,
      }),
    );
    await h.settle();
    expect(h.frames).toHaveLength(1);
    expect(JSON.parse(h.frames[0]?.data ?? 'null')).toEqual(messageDeltaFixture);
    expect(h.stream.lastSeq()).toBe(118);
  });

  it('未注入 transport 时默认走全局 fetch（浏览器生产路径）', async () => {
    const control = createControllableStream();
    const fetchMock = vi.fn(async (url: string, init: RequestInit) => {
      expect(url).toBe('/api/v1/sessions/sess-x/events');
      expect(init.signal).toBeInstanceOf(AbortSignal);
      return eventStreamResponse(control.stream);
    });
    vi.stubGlobal('fetch', fetchMock);
    try {
      const states: ConnState[] = [];
      const stream = new SessionEventStream({
        sessionId: 'sess-x',
        onFrame: () => undefined,
        onState: (state) => states.push(state),
      });
      const connected = stream.connect();
      await flush();
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/sessions/sess-x/events');
      expect(states).toContain('open');
      stream.close();
      await connected;
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe('lastSeq 跟踪（streaming-buffer 规则 1/24：seq 是续传与幂等的唯一锚点）', () => {
  it('数字 id 更新；非数字 id / 无 id 帧不改变水位', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.open(0);
    h.push(0, frameText({ id: 42, event: 'message_delta', data: 'a' }));
    await h.settle();
    expect(h.stream.lastSeq()).toBe(42);
    h.push(0, frameText({ id: 'abc', event: 'message_delta', data: 'b' }));
    h.push(0, frameText({ event: 'message_delta', data: 'c' }));
    await h.settle();
    expect(h.stream.lastSeq()).toBe(42);
    expect(h.frames).toHaveLength(3);
  });

  it('重放幂等（传输侧）：重复/回退 seq 帧照常透传给 dispatcher，水位单调不回退', async () => {
    // §5.2：seq 去重是 dispatcher 职责；传输层保证的是「不吞帧 + 水位不回退」，
    // 使重放/重复渲染已收内容不可发生（规则 24）。
    const h = createHarness();
    void h.stream.connect();
    await h.open(0);
    h.push(0, frameText({ id: 3, event: 'message_delta', data: 'x3' }));
    await h.settle();
    expect(h.stream.lastSeq()).toBe(3);
    // 服务端重放：同 seq 再来一遍 + 更小 seq
    h.push(0, frameText({ id: 3, event: 'message_delta', data: 'x3' }));
    h.push(0, frameText({ id: 2, event: 'message_delta', data: 'x2' }));
    await h.settle();
    expect(h.frames).toHaveLength(3); // 全部透传（含重复），由 dispatcher 幂等跳过
    expect(h.stream.lastSeq()).toBe(3); // 水位不因重放回退
  });

  it('未知事件类型透传且不破坏流（前向兼容的传输侧；忽略策略归 dispatcher，A4 §4.7）', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.open(0);
    h.push(0, frameText({ id: 7, event: 'brand_new_type_v2', data: { anything: true } }));
    await h.settle();
    expect(h.frames).toHaveLength(1);
    expect(h.frames[0]?.event).toBe('brand_new_type_v2');
    expect(h.stream.lastSeq()).toBe(7);
    expect(h.lastState()).toBe('open');
  });
});

describe('路径 B：发消息后 ensureCaughtUp(ack.event_seq)', () => {
  it('已追平（lastSeq >= minSeq）复用现有连接；落后则立即断开重连带 after_seq + Last-Event-ID', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.open(0);
    h.push(0, frameText({ id: 10, event: 'message_delta', data: 'x' }));
    await h.settle();

    h.stream.ensureCaughtUp(5); // 10 >= 5：复用，不新建连接
    h.stream.ensureCaughtUp(10); // 边界相等也复用
    await h.settle();
    expect(h.callCount()).toBe(1);
    expect(h.lastState()).toBe('open');

    h.stream.ensureCaughtUp(20); // 落后：立即断开重连（不等退避）
    await h.settle();
    expect(h.callCount()).toBe(2);
    expect(h.url(1)).toBe(`${EVENTS_URL}?after_seq=20`);
    expect(h.header(1, 'Last-Event-ID')).toBe('10'); // 契约：头优先，after_seq 兜底
    expect(h.signalOf(0)?.aborted).toBe(true); // 旧连接被 abort
    expect(h.states.filter((s) => s === 'connecting')).toHaveLength(2);

    await h.open(1);
    expect(h.lastState()).toBe('open');
  });

  it('无水位（lastSeq undefined）时 ensureCaughtUp 直接带 after_seq 重连', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.open(0); // open 但还没收到任何帧
    h.stream.ensureCaughtUp(42);
    await h.settle();
    expect(h.callCount()).toBe(2);
    expect(h.url(1)).toBe(`${EVENTS_URL}?after_seq=42`);
    expect(h.header(1, 'Last-Event-ID')).toBeUndefined();
  });
});

describe('路径 C：断线重连（Last-Event-ID + 指数退避）', () => {
  it('服务端关流 → reconnecting → 3s 后带 Last-Event-ID 重连；成功 open 重置退避', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.open(0);
    for (const seq of [1, 2, 3]) {
      h.push(0, frameText({ id: seq, event: 'message_delta', data: `d${seq}` }));
    }
    await h.settle();
    expect(h.stream.lastSeq()).toBe(3);

    h.attempts[0]?.control?.close(); // 服务端正常关流 = 断线语义
    await h.settle();
    expect(h.lastState()).toBe('reconnecting');

    await h.advance(2999);
    expect(h.callCount()).toBe(1); // 3s 起步（契约 retry: 3000 同值）
    await h.advance(1);
    expect(h.callCount()).toBe(2);
    expect(h.url(1)).toBe(EVENTS_URL); // 路径 C 不带 after_seq
    expect(h.header(1, 'Last-Event-ID')).toBe('3');

    await h.open(1);
    expect(h.lastState()).toBe('open');

    // 退避已重置：再次断线仍从 3s 起步（而非 6s）
    h.attempts[1]?.control?.error(new Error('boom'));
    await h.settle();
    expect(h.lastState()).toBe('reconnecting');
    await h.advance(3000);
    expect(h.callCount()).toBe(3);
    expect(h.header(2, 'Last-Event-ID')).toBe('3'); // 水位跨重连保留
  });

  it('退避序列 3s→6s→12s→24s→30s(cap)→30s', async () => {
    const responder: Responder = async () => {
      throw new Error('network down');
    };
    const h = createHarness({ responder });
    const connected = h.stream.connect();
    await h.settle();
    expect(h.callCount()).toBe(1);
    expect(h.states).toEqual(['connecting', 'reconnecting']);
    await expect(connected).resolves.toBeUndefined(); // 首次进入 reconnecting 即 settle

    const schedule: Array<[delayMs: number, expectedCalls: number]> = [
      [3000, 2],
      [6000, 3],
      [12000, 4],
      [24000, 5],
      [30000, 6], // cap
      [30000, 7], // cap 持续
    ];
    for (const [delayMs, expectedCalls] of schedule) {
      await h.advance(delayMs - 1);
      expect(h.callCount(), `t+${delayMs - 1}ms`).toBe(expectedCalls - 1);
      await h.advance(1);
      await h.settle();
      expect(h.callCount(), `t+${delayMs}ms`).toBe(expectedCalls);
    }
    expect(h.states.filter((s) => s === 'reconnecting').length).toBeGreaterThanOrEqual(6);
  });

  it('服务端 retry: 建议值成为退避起点（尊重契约 retry: 3000 语义）', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.open(0);
    h.push(0, retryText(5000));
    await h.settle();
    h.attempts[0]?.control?.error(new Error('drop'));
    await h.settle();
    expect(h.lastState()).toBe('reconnecting');
    await h.advance(4999);
    expect(h.callCount()).toBe(1);
    await h.advance(1);
    expect(h.callCount()).toBe(2); // 5s 而非默认 3s
  });

  it('retry: 0 / 非法值不采纳（防热循环重连）', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.open(0);
    h.push(0, retryText(0));
    await h.settle();
    h.attempts[0]?.control?.close();
    await h.settle();
    await h.advance(2999);
    expect(h.callCount()).toBe(1); // 仍是默认 3s 起步
    await h.advance(1);
    expect(h.callCount()).toBe(2);
  });
});

describe('心跳与连接建立超时', () => {
  it('open 后 30s 无任何字节 → abort 走重连；注释心跳刷新看门狗并回调 onHeartbeat', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.open(0);

    await h.advance(29_000);
    expect(h.lastState()).toBe('open'); // 29s 未超时

    h.push(0, commentText()); // `: keep-alive`
    await h.settle();
    expect(h.heartbeats.count).toBe(1);

    await h.advance(29_000);
    expect(h.lastState()).toBe('open'); // 心跳已刷新看门狗

    await h.advance(1000);
    expect(h.lastState()).toBe('reconnecting'); // 距最后字节 30s
    expect(h.signalOf(0)?.aborted).toBe(true);

    await h.advance(3000);
    expect(h.callCount()).toBe(2); // 正常进入重连
  });

  it('任意字节都刷新看门狗（业务帧同样续命）', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.open(0);
    for (let round = 0; round < 3; round += 1) {
      await h.advance(25_000);
      h.push(0, frameText({ id: round + 1, event: 'sandbox_output', data: 'tick' }));
      await h.settle();
      expect(h.lastState(), `round ${round}`).toBe('open');
    }
    expect(h.heartbeats.count).toBe(0); // 业务帧不算心跳信号
    expect(h.frames).toHaveLength(3);
  });

  it('连接建立超时 10s 无响应头 → abort 重连', async () => {
    const responder: Responder = (attempt) => pendingUntilAbort(attempt.call.init.signal);
    const h = createHarness({ responder });
    void h.stream.connect();
    await h.settle();
    expect(h.callCount()).toBe(1);

    await h.advance(9999);
    expect(h.lastState()).toBe('connecting');
    await h.advance(1);
    expect(h.lastState()).toBe('reconnecting');
    expect(h.signalOf(0)?.aborted).toBe(true);
    expect(warnText()).toContain('连接建立超时');

    await h.advance(3000);
    expect(h.callCount()).toBe(2); // 退避后重试
  });

  it('心跳/建立超时常量可注入（测试用短超时）', async () => {
    const h = createHarness({ heartbeatTimeoutMs: 100, connectTimeoutMs: 50 });
    void h.stream.connect();
    await h.advance(50); // 建立超时 50ms 生效
    expect(h.lastState()).toBe('reconnecting');
    await h.advance(3000);
    await h.open(1);
    expect(h.lastState()).toBe('open');
    await h.advance(100); // 心跳超时 100ms 生效
    expect(h.lastState()).toBe('reconnecting');
  });

  it('未提供 onHeartbeat 回调时收到注释心跳：不报错、流保持 open', async () => {
    const control = createControllableStream();
    const states: ConnState[] = [];
    const transport: Transport = () => Promise.resolve(eventStreamResponse(control.stream));
    const stream = new SessionEventStream({
      sessionId: 'sess-1',
      transport,
      onFrame: () => undefined,
      onState: (state) => states.push(state),
      // 故意不传 onHeartbeat
    });
    void stream.connect();
    await flush();
    expect(states[states.length - 1]).toBe('open');
    control.push(commentText());
    await flush();
    expect(states[states.length - 1]).toBe('open'); // 心跳看门狗照常刷新，无回调不崩
    expect(errorText()).toBe('');
    stream.close();
  });
});

describe('流前 REST 错误体（契约：流前错误以普通 REST 错误体返回）', () => {
  it('SESSION_NOT_FOUND → offline 终止，不无限重连；ensureCaughtUp 终态守卫', async () => {
    const h = createHarness();
    const connected = h.stream.connect();
    await h.resolveRestError(0, 404, 'SESSION_NOT_FOUND', '会话不存在');
    expect(h.states).toEqual(['connecting', 'offline']);
    await expect(connected).resolves.toBeUndefined();
    expect(errorText()).toContain('SESSION_NOT_FOUND');

    await h.advance(120_000);
    expect(h.callCount()).toBe(1); // 终止：不退避重连

    h.stream.ensureCaughtUp(1); // 终态守卫
    await h.settle();
    expect(h.callCount()).toBe(1);
    expect(warnText()).toContain('终态');

    h.stream.close(); // offline → close() 收敛清理
    expect(h.stream.state).toBe('closed');
  });

  it('NOT_FOUND（兜底 404）同样终止', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.resolveRestError(0, 404, 'NOT_FOUND');
    expect(h.lastState()).toBe('offline');
    await h.advance(60_000);
    expect(h.callCount()).toBe(1);
  });

  it('HTTP 500（INTERNAL）属瞬态 → 退避重连而非终止', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.resolveRestError(0, 500, 'INTERNAL', '内部错误');
    expect(h.lastState()).toBe('reconnecting');
    await h.advance(3000);
    expect(h.callCount()).toBe(2);
  });

  it('非 JSON 错误体 / 无错误体：按 HTTP 状态走瞬态失败', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.resolveRaw(0, new Response('gateway said no', { status: 502 }));
    expect(h.lastState()).toBe('reconnecting');
    await h.advance(3000);
    await h.resolveRaw(1, new Response(null, { status: 503 }));
    expect(h.callCount()).toBe(2);
    expect(h.lastState()).toBe('reconnecting');
  });

  it('200 但 Content-Type 非 text/event-stream → 瞬态失败（协议防线）', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.resolveRaw(
      0,
      new Response('{"code":"0"}', { status: 200, headers: { 'content-type': 'application/json' } }),
    );
    expect(h.lastState()).toBe('reconnecting');
    expect(warnText()).toContain('Content-Type'); // 瞬态失败走 warn（enterOffline 才走 error）
    await h.advance(3000);
    expect(h.callCount()).toBe(2);
  });

  it('200 + event-stream 但 body 为 null → 瞬态失败', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.resolveRaw(0, eventStreamResponse(null));
    expect(h.lastState()).toBe('reconnecting');
    await h.advance(3000);
    expect(h.callCount()).toBe(2);
  });

  it('200 且无 Content-Type 头 → 瞬态失败并标注「(缺失)」', async () => {
    const h = createHarness();
    void h.stream.connect();
    // null body + 无 content-type：get('content-type') 返回 null → `?? ''` → 缺失占位
    await h.resolveRaw(0, new Response(null, { status: 200 }));
    expect(h.lastState()).toBe('reconnecting');
    expect(warnText()).toContain('Content-Type');
    expect(warnText()).toContain('(缺失)');
  });

  it('流前错误体缺 message 字段 → offline 终止并占位「(无 message)」', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.resolveRaw(
      0,
      new Response(JSON.stringify({ code: 'SESSION_NOT_FOUND' }), {
        status: 404,
        headers: { 'content-type': 'application/json' },
      }),
    );
    expect(h.states).toEqual(['connecting', 'offline']);
    expect(errorText()).toContain('(无 message)');
  });
});

describe('生命周期：close 幂等 / stale 守卫 / 残帧', () => {
  it('open 中 close()：abort 传播、幂等、定时器清空、不再重连', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.open(0);
    h.stream.close();
    h.stream.close(); // 幂等：第二次静默
    expect(h.states.filter((s) => s === 'closed')).toHaveLength(1);
    expect(h.signalOf(0)?.aborted).toBe(true);
    expect(h.stream.state).toBe('closed');
    expect(vi.getTimerCount()).toBe(0); // 心跳/退避定时器全部清理

    await h.advance(60_000);
    expect(h.callCount()).toBe(1);
  });

  it('connecting 中 close()：迟到的响应被 stale 守卫忽略，不进 open', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.settle();
    h.stream.close();
    expect(h.states).toEqual(['connecting', 'closed']);
    expect(vi.getTimerCount()).toBe(0); // 连接建立定时器已清

    // 迟到响应：epoch 已失效 → 忽略并取消 body，不触发任何状态
    await h.open(0);
    expect(h.lastState()).toBe('closed');
    expect(h.frames).toHaveLength(0);
    h.push(0, frameText({ id: 1, event: 'e', data: 'x' }));
    await h.settle();
    expect(h.frames).toHaveLength(0);
  });

  it('connecting 中 ensureCaughtUp：立即换连接，旧响应迟到被忽略', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.settle();
    expect(h.lastState()).toBe('connecting');

    h.stream.ensureCaughtUp(5); // 无水位 → 断开在途连接立即重建
    await h.settle();
    expect(h.callCount()).toBe(2);
    expect(h.url(1)).toBe(`${EVENTS_URL}?after_seq=5`);

    await h.open(0); // 旧连接迟到 open：stale，被忽略
    expect(h.lastState()).toBe('connecting');
    await h.open(1); // 新连接正常 open
    expect(h.lastState()).toBe('open');
  });

  it('错误体读取在途时 ensureCaughtUp：迟到错误体被 stale 守卫丢弃，不误入 offline', async () => {
    // 真实竞态：response.text() 尚未读完（body 流未关闭），期间用户发消息触发 ensureCaughtUp
    const body = createControllableStream();
    const errResponse = new Response(body.stream, {
      status: 404,
      headers: { 'content-type': 'application/json' },
    });
    const h = createHarness();
    void h.stream.connect();
    await h.settle();
    h.attempts[0]?.deferred?.resolve(errResponse);
    await h.settle(); // 进入 !ok 分支并挂在 await response.text()
    expect(h.lastState()).toBe('connecting');

    h.stream.ensureCaughtUp(7); // 读取在途 → epoch++，发起新连接
    await h.settle();
    expect(h.callCount()).toBe(2);

    body.push('{"code":"SESSION_NOT_FOUND","message":"迟到"}');
    body.close(); // text() 此刻才 resolve → stale=true → return，不进 offline
    await h.settle();
    expect(h.states).not.toContain('offline');
    expect(h.states).not.toContain('reconnecting'); // 迟到错误体既不终止也不触发退避
  });

  it('流结束时存在残帧 → console.warn 显式标记（不静默丢），且不产帧、照常重连', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.open(0);
    h.push(0, 'id: 9\nevent: message_delta\ndata: {"seq":9'); // 帧中间被截断
    h.attempts[0]?.control?.close();
    await h.settle();
    expect(warnText()).toContain('残帧');
    expect(h.frames).toHaveLength(0);
    expect(h.lastState()).toBe('reconnecting');
    await h.advance(3000);
    expect(h.header(1, 'Last-Event-ID')).toBeUndefined(); // 残帧的 id 未被采纳
  });

  it('connect() 重入守卫：非 idle 状态二次调用被忽略', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.settle();
    void h.stream.connect();
    await h.settle();
    expect(h.callCount()).toBe(1);
    expect(warnText()).toContain('忽略');
  });

  it('消费回调异常隔离：onFrame 抛错不拖垮连接（transport 韧性）', async () => {
    const h = createHarness({
      onFrame: () => {
        throw new Error('dispatcher bug');
      },
    });
    void h.stream.connect();
    await h.open(0);
    h.push(0, frameText({ id: 1, event: 'message_delta', data: 'x' }));
    h.push(0, frameText({ id: 2, event: 'message_delta', data: 'y' }));
    await h.settle();
    expect(h.lastState()).toBe('open'); // 流不因消费侧 bug 断开
    expect(h.stream.lastSeq()).toBe(2); // 水位照常推进
    expect(errorText()).toContain('dispatcher bug');
  });

  it('fetch 网络层异常（reject）→ 瞬态失败退避重连', async () => {
    const h = createHarness();
    void h.stream.connect();
    await h.settle();
    h.attempts[0]?.deferred?.reject(new TypeError('Failed to fetch'));
    await h.settle();
    expect(h.lastState()).toBe('reconnecting');
    expect(warnText()).toContain('fetch 失败');
    await h.advance(3000);
    expect(h.callCount()).toBe(2);
  });

  it('重连中 close()：取消退避定时器，不再发起连接', async () => {
    const responder: Responder = async () => {
      throw new Error('down');
    };
    const h = createHarness({ responder });
    void h.stream.connect();
    await h.settle();
    expect(h.lastState()).toBe('reconnecting');
    h.stream.close();
    expect(vi.getTimerCount()).toBe(0);
    await h.advance(60_000);
    expect(h.callCount()).toBe(1);
  });
});

describe('StreamManager（App 级单流，A4 §4.3 会话切换 / 规则 5 单路复用）', () => {
  it('会话切换：先 close 旧流（旧 onState 收到 closed）再建新流', async () => {
    const attempts: Attempt[] = [];
    const transport: Transport = (url, init) => {
      const attempt: Attempt = { call: { url, init } };
      attempt.control = createControllableStream(init.signal);
      attempt.deferred = createDeferred<Response>();
      attempts.push(attempt);
      return attempt.deferred.promise;
    };
    const statesA: Array<[ConnState, string]> = [];
    const statesB: Array<[ConnState, string]> = [];
    const manager = new StreamManager();

    manager.connect('sess-a', { transport, onState: (s, id) => statesA.push([s, id]) });
    await flush();
    attempts[0]?.deferred?.resolve(eventStreamResponse(attempts[0]?.control?.stream ?? null));
    await flush();
    expect(manager.sessionId).toBe('sess-a');
    expect(manager.state).toBe('open');

    manager.connect('sess-b', { transport, onState: (s, id) => statesB.push([s, id]) });
    await flush();
    expect(statesA[statesA.length - 1]).toEqual(['closed', 'sess-a']);
    expect(attempts[0]?.call.init.signal?.aborted).toBe(true);
    expect(attempts).toHaveLength(2);
    expect(attempts[1]?.call.url).toBe('/api/v1/sessions/sess-b/events');
    expect(statesB[statesB.length - 1]).toEqual(['connecting', 'sess-b']);
    expect(manager.sessionId).toBe('sess-b');
    manager.close();
  });

  it('同会话且存活 → 复用不重建；终态后重建', async () => {
    const h = createHarness();
    const manager = new StreamManager();
    manager.connect('sess-1', { transport: makeRecordingTransport(h) });
    await flush();
    manager.connect('sess-1', { transport: makeRecordingTransport(h) }); // 重复挂载
    await flush();
    expect(h.callCount()).toBe(1); // 复用（不重放，规则 5）

    h.attempts[0]?.deferred?.resolve(restErrorResponse(404, 'SESSION_NOT_FOUND'));
    await flush();
    await flush();
    expect(manager.state).toBe('offline');

    manager.connect('sess-1', { transport: makeRecordingTransport(h) }); // 终态 → 重建
    await flush();
    expect(h.callCount()).toBe(2);
    manager.close();
  });

  it('同会话二次 connect（存活）不再静默丢弃：console.warn 显式提示，复用语义不变（F7）', async () => {
    const h = createHarness();
    const manager = new StreamManager();
    manager.connect('sess-1', { transport: makeRecordingTransport(h) });
    await flush();
    manager.connect('sess-1', {
      transport: makeRecordingTransport(h),
      onState: () => undefined, // 二次挂载携带的新回调
    });
    await flush();

    // 复用语义保持（规则 5）：不重建、不重放、不发新连接
    expect(h.callCount()).toBe(1);
    expect(manager.sessionId).toBe('sess-1');
    // 但不再静默：丢弃新回调必须显式告警（F7）
    expect(warnText()).toContain('同会话');
    expect(warnText()).toContain('sess-1');
    manager.close();
  });

  it('ensureCaughtUp 委托当前流；无活动流时 warn 不抛', async () => {
    const h = createHarness();
    const manager = new StreamManager();
    manager.ensureCaughtUp(3); // 无流
    expect(warnText()).toContain('无活动会话流');

    manager.connect('sess-1', { transport: makeRecordingTransport(h) });
    await flush();
    h.attempts[0]?.deferred?.resolve(eventStreamResponse(h.attempts[0]?.control?.stream ?? null));
    await flush();
    h.push(0, frameText({ id: 3, event: 'message_delta', data: 'x' }));
    await flush();
    manager.ensureCaughtUp(9);
    await flush();
    expect(h.callCount()).toBe(2);
    expect(h.url(1)).toBe(`${EVENTS_URL}?after_seq=9`);
    manager.close();
  });

  it('connect 不带回调（dispatcher/connStore 未接线期）：默认 noop 不崩', async () => {
    const h = createHarness();
    const manager = new StreamManager();
    manager.connect('sess-1', { transport: makeRecordingTransport(h) });
    await flush();
    h.attempts[0]?.deferred?.resolve(eventStreamResponse(h.attempts[0]?.control?.stream ?? null));
    await flush();
    h.push(0, frameText({ id: 1, event: 'e', data: 'x' }));
    await flush();
    expect(manager.state).toBe('open');
    manager.close();
    manager.close();
    expect(manager.state).toBe('idle'); // 无当前流时回落 idle
    expect(manager.sessionId).toBeUndefined();
  });

  it('模块导出 App 级单实例 streamManager', () => {
    expect(streamManager).toBeInstanceOf(StreamManager);
  });
});

/** 复用 harness 的默认 responder（记录 attempt + 可控流），供 manager 测试注入。 */
function makeRecordingTransport(h: Harness): Transport {
  return (url, init) => {
    const control = createControllableStream(init.signal);
    const deferred = createDeferred<Response>();
    h.attempts.push({ call: { url, init }, control, deferred });
    return deferred.promise;
  };
}
