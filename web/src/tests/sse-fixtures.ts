/**
 * SSE 假 transport 测试基建（A4 §10.1「SSE 客户端集成 = 注入 fake transport，
 * ReadableStream 按字节切片喂入（含故意切在多字节字符中间）」）。
 *
 * 提供四类工具：
 * 1. 帧文本构造：frameText / commentText / retryText —— 按契约三行帧格式拼装
 *    （contracts/src/lcode_contracts/rest.py 模块 docstring：`id: <seq>` +
 *    `event: <type>` + `data: <单行 JSON>`，注释心跳 `: keep-alive`，`retry: 3000`）。
 *    不复制 contracts/tests/fixtures 大文件——需要真实事件载荷的测试经
 *    `@contracts-fixtures` 别名按需小量直引（A4 §10.2 同源原则）。
 * 2. 字节级切块：encodeText / chunkBytes —— size 不与字符边界对齐时天然制造
 *    「多字节字符被从中间切开」（streaming-buffer 规则 4 必测边界）。
 * 3. 可控流：createControllableStream —— 手动 push/close/error 的 ReadableStream，
 *    并镜像真实 fetch 语义：传入 signal abort 时流以 AbortError 出错。
 * 4. 响应与延迟构造：eventStreamResponse / restErrorResponse / createDeferred /
 *    pendingUntilAbort —— 模拟 open、流前 REST 错误体、永不响应（连接建立超时）、
 *    迟到响应（stale 守卫）等场景。
 *
 * 本文件位于 src/tests/（vite.config coverage.exclude 已排除，不计入覆盖率口径），
 * 与 @/events 无相互 import（transport 用结构化类型解耦），仅供测试使用。
 */

/** 记录一次 transport 调用（url + init），供断言 after_seq / Last-Event-ID / signal。 */
export interface RecordedCall {
  readonly url: string;
  readonly init: RequestInit;
}

export interface SseFrameTextOptions {
  /** `id:` 行（省略则帧不带 id） */
  readonly id?: number | string;
  /** `event:` 行（省略则帧不带 event） */
  readonly event?: string;
  /** `data:` 载荷；object 自动 JSON.stringify（契约：data 单行 JSON），
   *  string 中的 \n 会拆成多个 data: 行（SSE 多行 data 语义） */
  readonly data: string | object;
  /** 行分隔符，默认 '\n'；传 '\r\n' 构造 CRLF 服务端 */
  readonly eol?: string;
  /** 是否以空行结束（帧分界），默认 true；false 构造「未完结帧」 */
  readonly terminate?: boolean;
}

/** 按契约格式拼装一帧 SSE 文本。 */
export function frameText(options: SseFrameTextOptions): string {
  const eol = options.eol ?? '\n';
  const data = typeof options.data === 'string' ? options.data : JSON.stringify(options.data);
  const lines: string[] = [];
  if (options.id !== undefined) lines.push(`id: ${options.id}`);
  if (options.event !== undefined) lines.push(`event: ${options.event}`);
  for (const dataLine of data.split('\n')) lines.push(`data: ${dataLine}`);
  if (options.terminate ?? true) lines.push('');
  return lines.join(eol) + eol;
}

/** 注释行（心跳）：契约形态 `: keep-alive`。 */
export function commentText(text = 'keep-alive', eol = '\n'): string {
  return `: ${text}${eol}`;
}

/** `retry:` 行（契约：连接建立即发 retry: 3000）。 */
export function retryText(ms: number, eol = '\n'): string {
  return `retry: ${ms}${eol}`;
}

const encoder = new TextEncoder();

export function encodeText(text: string): Uint8Array {
  return encoder.encode(text);
}

/**
 * 把文本编码后按 size 字节切块。size 小于多字节字符宽度时必然从字符中间切开
 * （CJK 3 字节、emoji 4 字节）——半包解析必测数据（streaming-buffer 规则 4）。
 */
export function chunkBytes(text: string, size: number): Uint8Array[] {
  if (size < 1) throw new Error(`chunkBytes: size 必须 >= 1（收到 ${size}）`);
  const bytes = encodeText(text);
  const chunks: Uint8Array[] = [];
  for (let offset = 0; offset < bytes.length; offset += size) {
    chunks.push(bytes.slice(offset, offset + size));
  }
  return chunks;
}

/** 与真实 fetch/undici 一致的 AbortError。 */
export function abortError(): DOMException {
  return new DOMException('The operation was aborted.', 'AbortError');
}

export interface ControllableStream {
  readonly stream: ReadableStream<Uint8Array>;
  /** 喂入一段文本或字节（不产生任何定时器，读取侧经 microtask 可见） */
  push(chunk: string | Uint8Array): void;
  /** 服务端正常关闭流（reader 收到 done） */
  close(): void;
  /** 流异常（reader.read() 以 reason 拒绝） */
  error(reason?: unknown): void;
}

/**
 * 手动控制的 ReadableStream。镜像真实 fetch 行为：signal abort → 流以 AbortError
 * 出错（否则假流上的 abort 测试与生产语义脱节）。所有控制方法在流已终结后静默
 * 忽略（close 幂等场景会重复触达）。
 */
export function createControllableStream(signal?: AbortSignal | null): ControllableStream {
  let controllerRef: ReadableStreamDefaultController<Uint8Array> | undefined;
  let finished = false;
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controllerRef = controller;
    },
    cancel() {
      finished = true;
    },
  });
  const guarded = (action: () => void): void => {
    if (finished || !controllerRef) return;
    try {
      action();
    } catch {
      // 流已 close/error：竞态下的重复控制无意义，标记终结即可
      finished = true;
    }
  };
  if (signal) {
    if (signal.aborted) {
      guarded(() => controllerRef?.error(abortError()));
    } else {
      signal.addEventListener('abort', () => guarded(() => controllerRef?.error(abortError())), {
        once: true,
      });
    }
  }
  return {
    stream,
    push(chunk) {
      guarded(() =>
        controllerRef?.enqueue(typeof chunk === 'string' ? encodeText(chunk) : chunk),
      );
    },
    close() {
      guarded(() => controllerRef?.close());
    },
    error(reason) {
      guarded(() => controllerRef?.error(reason ?? new Error('controllable stream error')));
    },
  };
}

export interface Deferred<T> {
  readonly promise: Promise<T>;
  resolve(value: T): void;
  reject(reason?: unknown): void;
}

export function createDeferred<T>(): Deferred<T> {
  let resolveFn: ((value: T) => void) | undefined;
  let rejectFn: ((reason?: unknown) => void) | undefined;
  const promise = new Promise<T>((resolve, reject) => {
    resolveFn = resolve;
    rejectFn = reject;
  });
  return {
    promise,
    resolve(value) {
      resolveFn?.(value);
    },
    reject(reason) {
      rejectFn?.(reason);
    },
  };
}

/**
 * 永不响应头的 transport 返回值：仅在 signal abort 时以 AbortError 拒绝
 * （与真实 fetch 一致）。用于「连接建立超时」用例。
 */
export function pendingUntilAbort(signal?: AbortSignal | null): Promise<Response> {
  return new Promise<Response>((_resolve, reject) => {
    if (!signal) return;
    if (signal.aborted) {
      reject(abortError());
      return;
    }
    signal.addEventListener('abort', () => reject(abortError()), { once: true });
  });
}

/** HTTP 200 + text/event-stream 的 SSE 响应。body=null 构造「200 但无 body」边界。 */
export function eventStreamResponse(body: ReadableStream<Uint8Array> | string | null): Response {
  return new Response(body, {
    status: 200,
    headers: { 'content-type': 'text/event-stream; charset=utf-8' },
  });
}

/** 流前 REST 错误体（契约 errors.ErrorBody：{"code","message"}）。 */
export function restErrorResponse(status: number, code: string, message = '错误'): Response {
  return new Response(JSON.stringify({ code, message }), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** 从 RequestInit.headers（三种 HeadersInit 形态）大小写不敏感读取一个头。 */
export function readHeader(init: RequestInit, name: string): string | undefined {
  const headers = init.headers;
  const target = name.toLowerCase();
  if (headers instanceof Headers) return headers.get(name) ?? undefined;
  if (Array.isArray(headers)) {
    for (const entry of headers) {
      if (entry[0]?.toLowerCase() === target) return entry[1];
    }
    return undefined;
  }
  if (headers) {
    for (const [key, value] of Object.entries(headers)) {
      if (key.toLowerCase() === target) return value;
    }
  }
  return undefined;
}
