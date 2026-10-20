import { create } from 'zustand';

import type { ConnState } from '@/events/sse';

/**
 * SSE 连接状态镜像（A4 §3.2 connStore 行 / §4.3 / §4.5）。
 *
 * - `state`：连接状态机镜像（events/sse.ts 的 onState 经接线方写入，驱动 toast 与
 *   「连接中断」提示，AC-003-3 / NFR-006）；
 * - `lastSeq`：**展示用**的已消费水位镜像。**去重绝不使用它**——传输层（sse.ts）
 *   在派发帧之前就推进自身水位，dispatcher 若以此为去重锚点会把新事件全部误丢；
 *   dispatcher 的去重水位是它自己的模块级变量（A4 §5.2，见 events/dispatcher.ts）。
 *   写入侧带单调守卫（不回退），故传输侧与 dispatcher 侧双写也保持幂等；
 * - `validationErrorCount` / `unknownEventTypeCount`：边界防御打点（A4 §4.7/§5.2）。
 *   `unknownEventTypeCount` 是对 §3.2 字段表的**补充**：未知事件类型（前向兼容忽略
 *   路径）与 zod/JSON 校验失败是两类不同信号，合成一个计数器会掩盖真实原因；
 * - `lastHeartbeatAt`：最近一次字节到达（含注释心跳）时间戳，「连接保持」提示数据源。
 */
export interface ConnStore {
  state: ConnState;
  lastSeq: number | null;
  validationErrorCount: number;
  unknownEventTypeCount: number;
  lastHeartbeatAt: number | null;
  setState: (state: ConnState) => void;
  /** 单调守卫：仅接受大于当前值的 seq（传输侧与 dispatcher 双写的幂等保护）。 */
  setLastSeq: (seq: number) => void;
  noteHeartbeat: (at?: number) => void;
  bumpValidationErrors: () => void;
  bumpUnknownEventTypes: () => void;
  reset: () => void;
}

interface ConnData {
  state: ConnState;
  lastSeq: number | null;
  validationErrorCount: number;
  unknownEventTypeCount: number;
  lastHeartbeatAt: number | null;
}

function createConnSlice(): ConnData {
  return {
    state: 'idle',
    lastSeq: null,
    validationErrorCount: 0,
    unknownEventTypeCount: 0,
    lastHeartbeatAt: null,
  };
}

export const useConnStore = create<ConnStore>((set) => ({
  ...createConnSlice(),
  setState: (state) => set({ state }),
  setLastSeq: (seq) => set((s) => (s.lastSeq !== null && s.lastSeq >= seq ? {} : { lastSeq: seq })),
  noteHeartbeat: (at = Date.now()) => set({ lastHeartbeatAt: at }),
  bumpValidationErrors: () => set((s) => ({ validationErrorCount: s.validationErrorCount + 1 })),
  bumpUnknownEventTypes: () => set((s) => ({ unknownEventTypeCount: s.unknownEventTypeCount + 1 })),
  reset: () => set(createConnSlice()),
}));
