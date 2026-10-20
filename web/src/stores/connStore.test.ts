/**
 * connStore 单测（A4 §3.2/§4.3/§5.2）。
 *
 * 重点：lastSeq 单调守卫（传输侧与 dispatcher 侧双写的幂等）——**去重不读它**，
 * 该守卫只保证镜像不回退；校验失败与未知类型是两条独立计数。
 */
import { beforeEach, describe, expect, it } from 'vitest';

import { useConnStore } from './connStore';

beforeEach(() => {
  useConnStore.getState().reset();
});

describe('connStore', () => {
  it('初始态：idle / 水位空 / 计数清零', () => {
    const state = useConnStore.getState();
    expect(state.state).toBe('idle');
    expect(state.lastSeq).toBeNull();
    expect(state.validationErrorCount).toBe(0);
    expect(state.unknownEventTypeCount).toBe(0);
    expect(state.lastHeartbeatAt).toBeNull();
  });

  it('setLastSeq 单调守卫：只接受更大的 seq，重复/回退是 no-op', () => {
    const store = useConnStore.getState();
    store.setLastSeq(10);
    expect(useConnStore.getState().lastSeq).toBe(10);

    useConnStore.getState().setLastSeq(10); // 重复
    useConnStore.getState().setLastSeq(9); // 回退
    expect(useConnStore.getState().lastSeq).toBe(10);

    useConnStore.getState().setLastSeq(11);
    expect(useConnStore.getState().lastSeq).toBe(11);
  });

  it('状态机镜像与心跳时间戳', () => {
    useConnStore.getState().setState('connecting');
    useConnStore.getState().setState('open');
    useConnStore.getState().noteHeartbeat(1234);
    expect(useConnStore.getState().state).toBe('open');
    expect(useConnStore.getState().lastHeartbeatAt).toBe(1234);
  });

  it('校验失败与未知类型分开计数（不同信号不混流）', () => {
    useConnStore.getState().bumpValidationErrors();
    useConnStore.getState().bumpValidationErrors();
    useConnStore.getState().bumpUnknownEventTypes();

    expect(useConnStore.getState().validationErrorCount).toBe(2);
    expect(useConnStore.getState().unknownEventTypeCount).toBe(1);
  });

  it('reset 回到初始态', () => {
    useConnStore.getState().setState('offline');
    useConnStore.getState().setLastSeq(7);
    useConnStore.getState().bumpValidationErrors();

    useConnStore.getState().reset();
    expect(useConnStore.getState()).toMatchObject({
      state: 'idle',
      lastSeq: null,
      validationErrorCount: 0,
      unknownEventTypeCount: 0,
      lastHeartbeatAt: null,
    });
  });
});
