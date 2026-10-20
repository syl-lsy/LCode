/**
 * rAF 定速泵（A4 §6.1；streaming-buffer 规则 8-11，frontend-pipeline §4）。
 *
 * 职责单一：**每帧最多执行一次 drain**（幂等排队），其余（定速/冲刷/上限）归
 * buffers.ts。三条降级路径（§6.1/§4.6）：
 * - 默认帧源 = requestAnimationFrame；
 * - `document.hidden`（页面隐藏）→ setTimeout(200ms) 兜底循环（rAF 在隐藏页被节流
 *   到 ~1Hz 甚至暂停，接收不受影响但渲染要按兜底节奏推进）；
 * - rAF 不可用（Node/Worker/测试环境）→ microtask 降级。
 * visibilitychange 恢复可见且有待执行帧时**立即补泵**（不等下一个 200ms 窗口）。
 *
 * 纯模块：不 import 任何 store（drain 回调由 buffers.ts 注册），便于单测注入假帧源。
 */

export type FrameCallback = () => void;

/** 帧源环境（可注入；默认真实环境见 createDefaultPumpEnvironment）。 */
export interface PumpEnvironment {
  /** 请求一帧，返回句柄（句柄可被 cancelFrame 取消） */
  readonly requestFrame: (cb: FrameCallback) => number;
  readonly cancelFrame: (handle: number) => void;
  readonly isHidden?: () => boolean;
  /** 订阅可见性变化（返回取消订阅函数）；缺省不订阅 */
  readonly subscribeVisibility?: (cb: () => void) => () => void;
}

/** 隐藏页兜底泵间隔（§6.1） */
export const HIDDEN_PUMP_MS = 200;

function hasAnimationFrame(): boolean {
  return typeof globalThis.requestAnimationFrame === 'function';
}

function documentHidden(): boolean {
  return typeof document !== 'undefined' && document.hidden === true;
}

/** 默认帧源：rAF → 隐藏页 setTimeout(200) → 无 rAF 环境 microtask。 */
export function createDefaultPumpEnvironment(): PumpEnvironment {
  /** 已取消的句柄（setTimeout/microtask 路径无法原生取消，用墓碑集合标记） */
  const tombstones = new Set<number>();
  /** 自建句柄 → rAF 原生句柄（rAF 路径可原生取消） */
  const nativeHandles = new Map<number, number>();
  let sequence = 0;

  const settle = (handle: number, cb: FrameCallback): void => {
    if (tombstones.delete(handle)) return;
    nativeHandles.delete(handle);
    cb();
  };

  return {
    requestFrame: (cb) => {
      const handle = ++sequence;
      if (documentHidden()) {
        setTimeout(() => settle(handle, cb), HIDDEN_PUMP_MS);
        return handle;
      }
      if (hasAnimationFrame()) {
        const native = globalThis.requestAnimationFrame(() => settle(handle, cb));
        nativeHandles.set(handle, native);
        return handle;
      }
      queueMicrotask(() => settle(handle, cb));
      return handle;
    },
    cancelFrame: (handle) => {
      const native = nativeHandles.get(handle);
      if (native !== undefined) {
        nativeHandles.delete(handle);
        if (typeof globalThis.cancelAnimationFrame === 'function') {
          globalThis.cancelAnimationFrame(native);
        }
        return;
      }
      tombstones.add(handle);
    },
    isHidden: documentHidden,
    subscribeVisibility: (cb) => {
      if (typeof document === 'undefined' || typeof document.addEventListener !== 'function') {
        return () => undefined;
      }
      document.addEventListener('visibilitychange', cb);
      return () => document.removeEventListener('visibilitychange', cb);
    },
  };
}

/** 帧泵：幂等排队 + 每帧一轮 drain + 同步 flush。 */
export class FramePump {
  private readonly environment: PumpEnvironment;
  private readonly listeners = new Set<FrameCallback>();
  private handle: number | null = null;
  private disposed = false;
  private readonly unsubscribe: (() => void) | undefined;

  constructor(environment: PumpEnvironment) {
    this.environment = environment;
    if (environment.subscribeVisibility) {
      // 回调由本泵持有：多泵实例各自订阅，不共享模块级可变状态
      this.unsubscribe = environment.subscribeVisibility(() => this.onVisibilityChange());
    }
  }

  /** 注册 drain 回调（返回退订）。 */
  add(listener: FrameCallback): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  get pending(): boolean {
    return this.handle !== null;
  }

  get listenerCount(): number {
    return this.listeners.size;
  }

  /** 请求一帧；已排队时不重复（每帧最多一轮 drain）。 */
  request(): void {
    if (this.disposed || this.handle !== null) return;
    this.handle = this.environment.requestFrame(() => {
      this.handle = null;
      this.tick();
    });
  }

  /** 同步立即 drain（终态冲刷；取消在途帧后立即执行，不等下一帧）。 */
  flush(): void {
    if (this.disposed) return;
    if (this.handle !== null) {
      this.environment.cancelFrame(this.handle);
      this.handle = null;
    }
    this.tick();
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    if (this.handle !== null) {
      this.environment.cancelFrame(this.handle);
      this.handle = null;
    }
    this.unsubscribe?.();
    this.listeners.clear();
  }

  private tick(): void {
    for (const listener of [...this.listeners]) {
      try {
        listener();
      } catch (error) {
        // 单个 drain 回调（通道）异常不影响其它通道与后续帧
        console.error('[scheduler] drain 回调异常（已隔离）:', error);
      }
    }
  }

  private onVisibilityChange(): void {
    if (this.disposed) return;
    if (this.environment.isHidden?.() === false && this.handle !== null) this.flush();
  }
}

/** 用默认环境或覆写项构造帧泵（测试注入假帧源的标准入口）。 */
export function createFramePump(overrides: Partial<PumpEnvironment> = {}): FramePump {
  return new FramePump({ ...createDefaultPumpEnvironment(), ...overrides });
}

/** App 级唯一帧泵（buffers.ts 注册 drain；测试用 createFramePump 注入假帧源）。 */
export const framePump = createFramePump();
