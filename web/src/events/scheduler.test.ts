/**
 * FramePump 单测（A4 §6.1；streaming-buffer 规则 8-11）。
 *
 * 覆盖：幂等排队（每帧最多一轮 drain）、同步 flush（取消在途帧）、drain 回调异常隔离、
 * visibilitychange 恢复即刻补泵、dispose 清场；以及默认帧源的三条降级路径
 * （rAF / 隐藏页 setTimeout(200) / 无 rAF 环境 microtask）。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import { createDefaultPumpEnvironment, createFramePump, FramePump, HIDDEN_PUMP_MS } from './scheduler';

interface ManualFrames {
  readonly pump: FramePump;
  /** 手动跑一轮已排队的帧（模拟浏览器下一帧） */
  run(): void;
  pendingCount(): number;
}

/** 手控帧泵：帧回调进队列，测试显式 run()——定速/次数断言完全确定。 */
function manualFrames(
  extra: { readonly isHidden?: () => boolean; readonly subscribeVisibility?: (cb: () => void) => () => void } = {},
): ManualFrames {
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
    ...extra,
  });
  return {
    pump,
    run: () => {
      const callbacks = [...pending.values()];
      pending.clear();
      for (const cb of callbacks) cb();
    },
    pendingCount: () => pending.size,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('FramePump：排队与 drain', () => {
  it('每帧最多一轮 drain：连续 request 只排一帧，run 后回落到可再排', () => {
    const frames = manualFrames();
    let drains = 0;
    frames.pump.add(() => {
      drains += 1;
    });

    frames.pump.request();
    frames.pump.request();
    frames.pump.request();
    expect(frames.pendingCount()).toBe(1);
    expect(frames.pump.pending).toBe(true);

    frames.run();
    expect(drains).toBe(1);
    expect(frames.pump.pending).toBe(false);

    frames.pump.request();
    frames.run();
    expect(drains).toBe(2);
  });

  it('flush() 同步 drain 并取消在途帧（终态冲刷不等下一帧）', () => {
    const frames = manualFrames();
    const seen: string[] = [];
    frames.pump.add(() => seen.push('drain'));

    frames.pump.request();
    frames.pump.flush();

    expect(seen).toEqual(['drain']);
    expect(frames.pendingCount()).toBe(0);
    frames.run(); // 已取消的帧即使被跑也不应再 drain
    expect(seen).toEqual(['drain']);
  });

  it('一个 drain 回调抛错不影响其它回调与后续帧（异常隔离）', () => {
    const frames = manualFrames();
    const seen: string[] = [];
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);

    frames.pump.add(() => {
      throw new Error('channel boom');
    });
    frames.pump.add(() => seen.push('healthy'));

    frames.pump.request();
    frames.run();
    expect(seen).toEqual(['healthy']);
    expect(errorSpy).toHaveBeenCalledTimes(1);

    frames.pump.request();
    frames.run();
    expect(seen).toEqual(['healthy', 'healthy']);
    errorSpy.mockRestore();
  });

  it('退订后不再收到 drain', () => {
    const frames = manualFrames();
    let drains = 0;
    const unsubscribe = frames.pump.add(() => {
      drains += 1;
    });
    unsubscribe();

    expect(frames.pump.listenerCount).toBe(0);
    frames.pump.request();
    frames.run();
    expect(drains).toBe(0);
  });

  it('visibilitychange 恢复可见且有待执行帧 → 立即补泵', () => {
    let hidden = true;
    // 持有订阅回调的引用（对象属性而非裸 let：避免 TS 对闭包赋值的收窄误判）
    const visibility: { cb: (() => void) | null } = { cb: null };
    const frames = manualFrames({
      isHidden: () => hidden,
      subscribeVisibility: (cb) => {
        visibility.cb = cb;
        return () => {
          visibility.cb = null;
        };
      },
    });
    let drains = 0;
    frames.pump.add(() => {
      drains += 1;
    });

    frames.pump.request(); // 隐藏期间的排帧
    expect(drains).toBe(0);

    hidden = false;
    visibility.cb?.();
    expect(drains).toBe(1); // 立即补泵，不等下一帧窗口

    hidden = true;
    frames.pump.request();
    visibility.cb?.();
    expect(drains).toBe(1); // 仍隐藏：不补泵
  });

  it('dispose() 取消在途帧并清空订阅', () => {
    const frames = manualFrames();
    let drains = 0;
    frames.pump.add(() => {
      drains += 1;
    });
    frames.pump.request();
    frames.pump.dispose();

    frames.run();
    expect(drains).toBe(0);
    expect(frames.pump.listenerCount).toBe(0);

    frames.pump.request(); // 已 dispose：request 变 no-op
    expect(frames.pendingCount()).toBe(0);
  });

  it('dispose/flush 幂等：重复调用是 no-op', () => {
    const frames = manualFrames();
    let drains = 0;
    frames.pump.add(() => {
      drains += 1;
    });
    frames.pump.dispose();
    frames.pump.dispose();
    frames.pump.flush(); // 已 dispose：flush 不再 drain
    expect(drains).toBe(0);
  });

  it('无 subscribeVisibility 的环境：构造不订阅，可见性路径不触发', () => {
    let sequence = 0;
    const pending = new Map<number, () => void>();
    const pump = new FramePump({
      requestFrame: (cb) => {
        const handle = ++sequence;
        pending.set(handle, cb);
        return handle;
      },
      cancelFrame: (handle) => {
        pending.delete(handle);
      },
    });
    let drains = 0;
    pump.add(() => {
      drains += 1;
    });

    pump.request();
    pump.flush();
    expect(drains).toBe(1);
  });

  it('可见性回调在无排队帧时触发 → 不 flush（幂等空转）', () => {
    const hidden = false;
    const visibility: { cb: (() => void) | null } = { cb: null };
    const frames = manualFrames({
      isHidden: () => hidden,
      subscribeVisibility: (cb) => {
        visibility.cb = cb;
        return () => {
          visibility.cb = null;
        };
      },
    });
    let drains = 0;
    frames.pump.add(() => {
      drains += 1;
    });

    visibility.cb?.(); // 无在途帧
    expect(drains).toBe(0);

    frames.pump.request();
    frames.pump.dispose();
    visibility.cb?.(); // 已 dispose
    expect(drains).toBe(0);
  });

  it('cancelFrame 在无原生 cancelAnimationFrame 的环境走墓碑集合', () => {
    const native = globalThis.cancelAnimationFrame;
    vi.stubGlobal('cancelAnimationFrame', undefined);
    const environment = createDefaultPumpEnvironment();
    const pump = new FramePump(environment);
    let drains = 0;
    pump.add(() => {
      drains += 1;
    });

    pump.request();
    pump.flush();
    expect(drains).toBe(1);
    pump.dispose();
    vi.stubGlobal('cancelAnimationFrame', native);
  });
});

describe('默认帧源三条降级路径（createDefaultPumpEnvironment）', () => {
  it('无 rAF 环境 → microtask 降级（Node/Worker）', async () => {
    const environment = createDefaultPumpEnvironment();
    const pump = new FramePump(environment);
    let drains = 0;
    pump.add(() => {
      drains += 1;
    });

    pump.request();
    expect(drains).toBe(0);
    await Promise.resolve(); // microtask 落定
    expect(drains).toBe(1);
    pump.dispose();
  });

  it('document.hidden → setTimeout(200ms) 兜底泵 + cancel 生效', () => {
    vi.useFakeTimers();
    vi.stubGlobal('document', { hidden: true, addEventListener: () => undefined, removeEventListener: () => undefined });
    const pump = new FramePump(createDefaultPumpEnvironment());
    let drains = 0;
    pump.add(() => {
      drains += 1;
    });

    pump.request();
    vi.advanceTimersByTime(HIDDEN_PUMP_MS - 1);
    expect(drains).toBe(0);
    vi.advanceTimersByTime(1);
    expect(drains).toBe(1);

    pump.request();
    pump.flush(); // 取消隐藏页兜底定时器并立即 drain
    expect(drains).toBe(2);
    vi.advanceTimersByTime(HIDDEN_PUMP_MS);
    expect(drains).toBe(2); // 已取消：不再补一次
    pump.dispose();
  });

  it('rAF 可用 → 走 requestAnimationFrame，可原生取消', () => {
    const callbacks = new Map<number, () => void>();
    let sequence = 0;
    let cancelled = 0;
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      const handle = ++sequence;
      callbacks.set(handle, () => cb(0));
      return handle;
    });
    vi.stubGlobal('cancelAnimationFrame', (handle: number) => {
      cancelled += 1;
      callbacks.delete(handle);
    });

    const pump = new FramePump(createDefaultPumpEnvironment());
    let drains = 0;
    pump.add(() => {
      drains += 1;
    });

    pump.request();
    expect(callbacks.size).toBe(1);
    for (const cb of [...callbacks.values()]) cb();
    callbacks.clear();
    expect(drains).toBe(1);

    pump.request();
    pump.flush();
    expect(cancelled).toBe(1);
    expect(drains).toBe(2);
    pump.dispose();
  });
});
