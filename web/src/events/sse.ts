/**
 * SSE 客户端（A4 §4：SessionEventStream + streamManager App 级单实例）。
 *
 * 对齐 streaming-buffer「不可妥协清单」（传输与接收，规则 1-7）：
 * - 规则 1：帧 id(seq) 是去重/续传/幂等的唯一锚点 → `lastSeq()` 水位，**单调不回退**
 *   （重放帧照常透传，seq 去重归 dispatcher，§5.2 / 规则 24）；
 * - 规则 2：三行帧解析归 parser.ts；15s 级心跳对应此处 30s 无字节看门狗（2×心跳+余量）；
 * - 规则 3：断线续传带 `Last-Event-ID`（契约头优先），`?after_seq=` 查询参数兜底；
 * - 规则 5：单路复用一条事件流 → StreamManager 强制 App 级唯一活跃流；
 * - 规则 24：重连幂等的传输侧保证 = 不吞帧 + 水位不回退 + 续传锚点齐备。
 *
 * 三条连接路径（§4.2）：
 * - A 打开/刷新：`connect()` 无水位 → 服务端全量重放（batch 快速路径归 §3.3，M2 任务 4）；
 * - B 发消息后：`ensureCaughtUp(ack.event_seq)` → 已追平复用；否则立即断开重连带 after_seq；
 * - C 断线重连：指数退避（3s 起步 ×2 cap 30s；服务端 `retry:` 建议覆盖起步值；open 重置）。
 *
 * 错误语义（§4.1/§4.3/§4.7）：
 * - 流前 REST 错误体（ErrorBody）且 code 为终态（SESSION_NOT_FOUND/NOT_FOUND）→ offline
 *   终止，不无限重连；其余 HTTP 错误/网络失败/超时/流断 → 瞬态失败进退避；
 * - 未知 event 类型照常透传——「忽略 + console.warn + connStore 打点」策略归 dispatcher
 *   （A4 §4.7，M2 任务 4）；传输层不做内容判断，前向兼容不破坏流。
 *
 * 边界：本模块只管传输——JSON.parse/zod 校验/seq 去重/store 写入归 dispatcher（§5.2）；
 * connStore 同步（§4.3「每次状态变化同步 connStore」）经 onState 回调由接线方完成
 * （M2 任务 4 之前默认 noop）。默认 transport 为全局 fetch（相对 URL 依赖浏览器
 * baseURI；Electron/测试经注入替换）。
 */

import { SseLineParser } from './parser';
import type { SseFrame, SseParserSignal } from './parser';

export type { SseFrame } from './parser';

/** 连接状态机（A4 §4.3）。offline = 流前终态错误；closed = 用户侧关闭。两者均为终态。 */
export type ConnState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'offline' | 'closed';

/** 可注入 transport（默认 fetch；测试用假 transport，Electron 未来可换 net 模块）。 */
export type Transport = (url: string, init: RequestInit) => Promise<Response>;

export interface BackoffConfig {
  /** 首次重连延迟（服务端 `retry:` 建议值到达后覆盖此值，契约 retry: 3000） */
  readonly base: number;
  /** 倍率（设计冻结 ×2） */
  readonly factor: number;
  /** 上限 */
  readonly cap: number;
}

export interface SessionEventStreamOptions {
  readonly sessionId: string;
  readonly transport?: Transport;
  /** 完整帧透传出口（→ dispatcher，M2 任务 4 接线；消费侧异常被隔离，不影响连接） */
  readonly onFrame: (frame: SseFrame) => void;
  /** 状态迁移回调（→ connStore，驱动 toast 与「连接中断」提示，AC-003-3） */
  readonly onState: (state: ConnState) => void;
  /** 注释心跳行回调（「连接保持」提示的数据源，NFR-006） */
  readonly onHeartbeat?: () => void;
  /** open 后无任何字节（含注释行）的看门狗，默认 30s（§4.3：2×心跳+余量） */
  readonly heartbeatTimeoutMs?: number;
  /** connecting 无响应头超时，默认 10s（§4.3） */
  readonly connectTimeoutMs?: number;
  readonly backoff?: Partial<BackoffConfig>;
}

/** 终态 REST 错误码：会话已不存在，重连无意义 → offline 终止（§4.3 状态机）。 */
const TERMINAL_REST_CODES: ReadonlySet<string> = new Set(['SESSION_NOT_FOUND', 'NOT_FOUND']);

const DEFAULT_HEARTBEAT_TIMEOUT_MS = 30_000;
const DEFAULT_CONNECT_TIMEOUT_MS = 10_000;
const DEFAULT_BACKOFF: BackoffConfig = { base: 3_000, factor: 2, cap: 30_000 };

/** 契约：帧 id = 十进制 seq（rest.py docstring）。非数字 id 不采纳为水位。 */
const SEQ_DIGITS = /^\d+$/;

interface RestErrorBodyLike {
  readonly code?: unknown;
  readonly message?: unknown;
}

async function readBodyTextSafely(response: Response): Promise<string> {
  try {
    return await response.text();
  } catch {
    return ''; // body 读取失败（连接层已断等）：退回按 HTTP 状态处理
  }
}

async function cancelBodySafely(response: Response): Promise<void> {
  try {
    await response.body?.cancel();
  } catch {
    // 流已出错/已关闭：取消失败无碍，本连接已被 stale 守卫废弃
  }
}

function parseRestErrorBody(bodyText: string): RestErrorBodyLike | undefined {
  if (bodyText === '') return undefined;
  try {
    const parsed: unknown = JSON.parse(bodyText);
    if (typeof parsed === 'object' && parsed !== null) return parsed as RestErrorBodyLike;
  } catch {
    // 非 JSON 错误体（网关裸文本等）：走 HTTP 状态瞬态失败路径
  }
  return undefined;
}

function asString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

export class SessionEventStream {
  private readonly sessionId: string;
  private readonly transport: Transport;
  private readonly onFrame: (frame: SseFrame) => void;
  private readonly onStateCb: (state: ConnState) => void;
  private readonly onHeartbeat?: () => void;
  private readonly heartbeatTimeoutMs: number;
  private readonly connectTimeoutMs: number;
  private readonly backoff: BackoffConfig;

  private stateValue: ConnState = 'idle';
  /** close() 置位：一切异步回调据此熄火（规则 21 abort 全链路传播） */
  private disposed = false;
  /** 连接尝试代号：每次作废在途连接自增，异步续体凭它识别自己是否已过期（stale 守卫） */
  private epoch = 0;
  /** 连续失败次数（open 成功归零 = 退避重置） */
  private attempt = 0;
  private lastSeqValue: number | undefined;
  /** 显式续传水位（connect(afterSeq) / ensureCaughtUp），作 Last-Event-ID 缺失时的查询兜底 */
  private resumeAfterSeq: number | undefined;
  /** 服务端 `retry:` 建议（>0 才采纳，防热循环） */
  private serverRetryMs: number | undefined;
  private activeController: AbortController | undefined;
  private connectTimer: ReturnType<typeof setTimeout> | undefined;
  private heartbeatTimer: ReturnType<typeof setTimeout> | undefined;
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  /** connect() 返回 Promise 的 resolve：首个非 connecting 状态（open/reconnecting/offline/closed）兑现 */
  private firstSettled: (() => void) | undefined;

  constructor(options: SessionEventStreamOptions) {
    this.sessionId = options.sessionId;
    this.transport = options.transport ?? ((url, init) => fetch(url, init));
    this.onFrame = options.onFrame;
    this.onStateCb = options.onState;
    this.onHeartbeat = options.onHeartbeat;
    this.heartbeatTimeoutMs = options.heartbeatTimeoutMs ?? DEFAULT_HEARTBEAT_TIMEOUT_MS;
    this.connectTimeoutMs = options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS;
    const backoff = options.backoff ?? {};
    this.backoff = {
      base: backoff.base ?? DEFAULT_BACKOFF.base,
      factor: backoff.factor ?? DEFAULT_BACKOFF.factor,
      cap: backoff.cap ?? DEFAULT_BACKOFF.cap,
    };
  }

  get state(): ConnState {
    return this.stateValue;
  }

  /** 已收帧水位（Last-Event-ID 续传锚点；单调不回退，规则 1/24）。 */
  lastSeq(): number | undefined {
    return this.lastSeqValue;
  }

  /**
   * 发起连接（路径 A：无参 = 无水位全量重放）。实例生命周期内仅可调用一次
   * （单流语义，重连/追平由内部与 ensureCaughtUp 负责）。
   * 返回的 Promise 在**首次尝试落定**（open / reconnecting / offline / closed）时
   * 兑现，不拒绝——后续状态变化走 onState。
   */
  connect(afterSeq?: number): Promise<void> {
    if (this.stateValue !== 'idle') {
      console.warn(
        `[sse] connect() 忽略：连接已处于 ${this.stateValue} 状态（单实例单次 connect；追平用 ensureCaughtUp）`,
      );
      return Promise.resolve();
    }
    this.resumeAfterSeq = afterSeq;
    return new Promise<void>((resolve) => {
      this.firstSettled = resolve;
      void this.runConnection();
    });
  }

  /**
   * 路径 B：发消息后以 ack.event_seq 对齐水位（§4.2）。
   * 已追平（lastSeq >= minSeq）→ 复用现有连接；否则立即断开重连（不等退避——
   * 用户刚发了消息），带 `?after_seq=minSeq` + `Last-Event-ID: lastSeq`（契约头优先，
   * 查询参数兜底），发送期间产生的事件由服务端重放补齐，不丢。
   */
  ensureCaughtUp(minSeq: number): void {
    if (this.disposed || this.stateValue === 'closed' || this.stateValue === 'offline') {
      console.warn(`[sse] ensureCaughtUp(${minSeq}) 忽略：连接已处终态（${this.stateValue}）`);
      return;
    }
    if (this.lastSeqValue !== undefined && this.lastSeqValue >= minSeq) return;
    this.resumeAfterSeq = minSeq;
    this.invalidateActive();
    void this.runConnection();
  }

  /** 用户切换会话/卸载：abort 传播 + 清全部定时器（规则 21）。幂等。 */
  close(): void {
    if (this.stateValue === 'closed') return;
    this.disposed = true;
    this.invalidateActive();
    this.setState('closed');
  }

  // ---------------------------------------------------------------- 内部：连接循环

  private async runConnection(): Promise<void> {
    if (this.disposed) return;
    const myEpoch = this.epoch;
    const stale = (): boolean => this.disposed || myEpoch !== this.epoch;

    const controller = new AbortController();
    this.activeController = controller;
    this.setState('connecting');

    const headers: Record<string, string> = { Accept: 'text/event-stream' };
    if (this.lastSeqValue !== undefined) headers['Last-Event-ID'] = String(this.lastSeqValue);
    const url = this.buildUrl();

    // 连接建立超时：10s 无响应头 → abort 走重连（§4.3）
    this.connectTimer = setTimeout(() => {
      this.connectTimer = undefined;
      if (stale()) return;
      this.handleTransientFailure(myEpoch, `连接建立超时（${this.connectTimeoutMs}ms 无响应头）`);
    }, this.connectTimeoutMs);

    let response: Response;
    try {
      response = await this.transport(url, { method: 'GET', headers, signal: controller.signal });
    } catch (error) {
      if (stale()) return; // 超时/用户 close 触发的 AbortError 等：已有新归属，熄火
      this.clearConnectTimer();
      this.handleTransientFailure(myEpoch, 'fetch 失败（网络层）', error);
      return;
    }
    if (stale()) {
      // 迟到响应：本尝试已被 close/ensureCaughtUp 作废——取消 body，不触发任何状态
      await cancelBodySafely(response);
      return;
    }
    this.clearConnectTimer();

    if (!response.ok) {
      const bodyText = await readBodyTextSafely(response);
      if (stale()) return;
      const restError = parseRestErrorBody(bodyText);
      const code = asString(restError?.code);
      if (code !== undefined && TERMINAL_REST_CODES.has(code)) {
        this.enterOffline(code, response.status, asString(restError?.message));
        return;
      }
      this.handleTransientFailure(
        myEpoch,
        `HTTP ${response.status}${code === undefined ? '' : ` code=${code}`}`,
      );
      return;
    }

    const contentType = response.headers.get('content-type') ?? '';
    if (!contentType.includes('text/event-stream')) {
      await cancelBodySafely(response);
      if (stale()) return;
      this.handleTransientFailure(myEpoch, `Content-Type 非 text/event-stream：${contentType || '(缺失)'}`);
      return;
    }
    if (!response.body) {
      this.handleTransientFailure(myEpoch, '200 响应无 body');
      return;
    }

    // ---- open：退避重置 + 心跳看门狗 + 读循环
    this.attempt = 0;
    this.setState('open');
    this.armHeartbeat(myEpoch);

    const parser = new SseLineParser();
    const reader = response.body.getReader();
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (stale()) return;
        if (done) break;
        if (value === undefined) continue;
        this.armHeartbeat(myEpoch); // 任何字节都刷新看门狗（§4.3「未收到任何字节（含注释行）」）
        for (const signal of parser.push(value)) {
          if (stale()) return;
          this.handleSignal(signal);
        }
      }
      const endResult = parser.end();
      if (endResult.truncated) {
        // 帧中间被截断：显式标记（parser.end 契约），不静默丢——缺口由重连重放补齐
        console.warn(
          `[sse] 流结束时存在残帧（不静默丢，重连重放补齐）：残行=${JSON.stringify(endResult.partialLine)}，已聚合半帧=${endResult.frameStarted}`,
        );
      }
      if (stale()) return;
      this.handleTransientFailure(myEpoch, '服务端关闭了事件流');
    } catch (error) {
      if (stale()) return;
      this.handleTransientFailure(myEpoch, '事件流读取失败', error);
    }
  }

  private buildUrl(): string {
    const base = `/api/v1/sessions/${encodeURIComponent(this.sessionId)}/events`;
    return this.resumeAfterSeq === undefined ? base : `${base}?after_seq=${this.resumeAfterSeq}`;
  }

  private handleSignal(signal: SseParserSignal): void {
    switch (signal.kind) {
      case 'frame': {
        const frame = signal.frame;
        if (frame.id !== undefined && SEQ_DIGITS.test(frame.id)) {
          const seq = Number(frame.id);
          // 单调不回退：重放/重复帧不拉低续传水位（规则 24；seq 去重归 dispatcher §5.2）
          if (this.lastSeqValue === undefined || seq > this.lastSeqValue) this.lastSeqValue = seq;
        }
        this.guard('onFrame', () => this.onFrame(frame));
        break;
      }
      case 'heartbeat': {
        const onHeartbeat = this.onHeartbeat;
        if (onHeartbeat) this.guard('onHeartbeat', () => onHeartbeat());
        break;
      }
      case 'retry':
        // 尊重服务端建议（契约 retry: 3000）；<=0 视为非法，防热循环重连
        if (signal.retryMs > 0) this.serverRetryMs = signal.retryMs;
        break;
    }
  }

  /** 心跳看门狗：open 后任何字节重置；到期 → abort 走重连（§4.3，30s = 2×心跳+余量）。 */
  private armHeartbeat(myEpoch: number): void {
    this.clearHeartbeatTimer();
    this.heartbeatTimer = setTimeout(() => {
      this.heartbeatTimer = undefined;
      if (this.disposed || myEpoch !== this.epoch) return;
      this.handleTransientFailure(myEpoch, `心跳超时（${this.heartbeatTimeoutMs}ms 未收到任何字节）`);
    }, this.heartbeatTimeoutMs);
  }

  /**
   * 瞬态失败统一出口：作废在途连接（abort 传播）→ 退避重连。
   * epoch 守卫防止过期尝试的失败回调重复入账。
   */
  private handleTransientFailure(myEpoch: number, reason: string, error?: unknown): void {
    if (this.disposed || myEpoch !== this.epoch) return;
    this.invalidateActive();
    if (error === undefined) console.warn(`[sse] 连接失败（${reason}），进入退避重连`);
    else console.warn(`[sse] 连接失败（${reason}），进入退避重连:`, error);
    this.scheduleReconnect();
  }

  /** 流前终态错误（SESSION_NOT_FOUND 等）：offline 终止，不无限重连（§4.3）。 */
  private enterOffline(code: string, status: number, message: string | undefined): void {
    this.invalidateActive();
    console.error(
      `[sse] 流前 REST 错误（HTTP ${status}，code=${code}）：${message ?? '(无 message)'} —— offline 终止，不再重连`,
    );
    this.setState('offline');
  }

  /** 指数退避：base(或服务端 retry 建议) × factor^attempt，cap 封顶；open 已在外层归零 attempt。 */
  private scheduleReconnect(): void {
    if (this.disposed) return;
    this.setState('reconnecting');
    const base = this.serverRetryMs ?? this.backoff.base;
    const delay = Math.min(base * this.backoff.factor ** this.attempt, this.backoff.cap);
    this.attempt += 1;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = undefined;
      if (this.disposed) return;
      void this.runConnection();
    }, delay);
  }

  /** 作废在途连接：epoch++（stale 化所有异步续体）+ 清定时器 + abort。 */
  private invalidateActive(): void {
    this.epoch += 1;
    this.clearConnectTimer();
    this.clearHeartbeatTimer();
    this.clearReconnectTimer();
    const controller = this.activeController;
    this.activeController = undefined;
    controller?.abort();
  }

  private clearConnectTimer(): void {
    if (this.connectTimer !== undefined) {
      clearTimeout(this.connectTimer);
      this.connectTimer = undefined;
    }
  }

  private clearHeartbeatTimer(): void {
    if (this.heartbeatTimer !== undefined) {
      clearTimeout(this.heartbeatTimer);
      this.heartbeatTimer = undefined;
    }
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== undefined) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = undefined;
    }
  }

  private setState(next: ConnState): void {
    if (this.stateValue === next) return; // 去重：同态迁移不外发
    this.stateValue = next;
    if (next !== 'connecting') this.settleFirst();
    this.guard('onState', () => this.onStateCb(next));
  }

  private settleFirst(): void {
    const resolve = this.firstSettled;
    if (resolve) {
      this.firstSettled = undefined;
      resolve();
    }
  }

  /** 消费回调异常隔离：dispatcher/store 的 bug 不得拖垮传输连接。 */
  private guard(label: string, fn: () => void): void {
    try {
      fn();
    } catch (error) {
      console.error(`[sse] ${label} 回调异常（已隔离，不影响连接）:`, error);
    }
  }
}

// ---------------------------------------------------------------- App 级单实例管理器

export interface StreamConnectOverrides {
  readonly transport?: Transport;
  /** 默认 noop——dispatcher 接线（M2 任务 4）之前帧被安全丢弃，不崩 */
  readonly onFrame?: (frame: SseFrame) => void;
  /** 带 sessionId 的状态回调（connStore 接线用；A4 §4.3） */
  readonly onState?: (state: ConnState, sessionId: string) => void;
  readonly onHeartbeat?: () => void;
  readonly heartbeatTimeoutMs?: number;
  readonly connectTimeoutMs?: number;
  readonly backoff?: Partial<BackoffConfig>;
  /** 首连水位（路径 B 冷启动变体；缺省 = 路径 A 无水位全量重放） */
  readonly afterSeq?: number;
}

const noopFrame = (): void => undefined;

/**
 * App 级单流管理器（§4.3「会话切换先 close 旧流再建新流」；规则 5 单路复用）。
 * 路由/会话切换调用 connect(sessionId)，发消息后调用 ensureCaughtUp(ack.event_seq)。
 */
export class StreamManager {
  private current: SessionEventStream | undefined;
  private currentSessionId: string | undefined;

  get sessionId(): string | undefined {
    return this.currentSessionId;
  }

  get state(): ConnState {
    return this.current?.state ?? 'idle';
  }

  connect(sessionId: string, overrides: StreamConnectOverrides = {}): void {
    const existing = this.current;
    if (existing && this.currentSessionId === sessionId) {
      const state = existing.state;
      if (state !== 'closed' && state !== 'offline') {
        // F7：同会话且存活复用现有流（单路复用，规则 5，不重建不重放）；但本次传入的
        // 新回调（onFrame/onState 等）不会被安装——显式告警，不再静默丢弃（排查线索）。
        console.warn(
          `[sse] connect(${sessionId}) 忽略：同会话流已存活（${state} 状态），复用不重建；本次传入的 onFrame/onState 等新回调未安装（单路复用，规则 5）`,
        );
        return;
      }
    }
    this.close();
    const { afterSeq, onState, onFrame, onHeartbeat, transport, heartbeatTimeoutMs, connectTimeoutMs, backoff } =
      overrides;
    const stream = new SessionEventStream({
      sessionId,
      transport,
      onFrame: onFrame ?? noopFrame,
      onState: (state) => {
        onState?.(state, sessionId);
      },
      onHeartbeat,
      heartbeatTimeoutMs,
      connectTimeoutMs,
      backoff,
    });
    this.current = stream;
    this.currentSessionId = sessionId;
    void stream.connect(afterSeq);
  }

  ensureCaughtUp(minSeq: number): void {
    if (!this.current) {
      console.warn(`[sse] ensureCaughtUp(${minSeq}) 忽略：无活动会话流（需先 connect(sessionId)）`);
      return;
    }
    this.current.ensureCaughtUp(minSeq);
  }

  close(): void {
    const current = this.current;
    this.current = undefined;
    this.currentSessionId = undefined;
    current?.close();
  }
}

/** App 级唯一实例（A4 §4.5）：由路由守卫/会话切换与发消息链路调用。 */
export const streamManager = new StreamManager();
