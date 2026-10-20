// @vitest-environment jsdom
/**
 * useStickToBottom 四件套（A4 §6.4；规则 19/20）：jsdom 无真实滚动布局，
 * 在 hook 持有的真实元素上 defineProperty 伪造 scrollHeight/scrollTop/clientHeight
 * 驱动几何（React 持有该 DOM 节点，属性描述符不被覆盖）。
 *
 * 注意：hook 返回普通对象（每次渲染换新），断言必须重读 holder.stick（不能缓存快照，
 * 否则 atBottom/unreadCount 是旧渲染的值）。
 */
import { act, render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { STICK_THRESHOLD_PX, useStickToBottom } from './useStickToBottom';
import '../../../tests/test-utils';

interface FakeScroll {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
}

interface HostHandle {
  /** 当前渲染的 hook 返回值（每次渲染重读） */
  read: () => ReturnType<typeof useStickToBottom>;
  el: HTMLDivElement;
}

/** 渲染挂 ref 的宿主组件并把伪造几何装到 hook 持有的真实元素上。 */
function renderHost(geometry: FakeScroll): HostHandle {
  const holder: { stick?: ReturnType<typeof useStickToBottom> } = {};
  function Host() {
    holder.stick = useStickToBottom();
    return <div ref={holder.stick.containerRef} onScroll={holder.stick.onScroll} data-testid="host" />;
  }
  render(<Host />);
  const read = (): ReturnType<typeof useStickToBottom> => {
    const stick = holder.stick;
    if (stick === undefined) throw new Error('stick 未初始化');
    return stick;
  };
  const el = read().containerRef.current;
  if (el === null) throw new Error('containerRef 未挂载');
  Object.defineProperties(el, {
    scrollHeight: { get: () => geometry.scrollHeight, configurable: true },
    clientHeight: { get: () => geometry.clientHeight, configurable: true },
    scrollTop: {
      get: () => geometry.scrollTop,
      set: (value: number) => {
        geometry.scrollTop = value;
      },
      configurable: true,
    },
  });
  return { read, el };
}

describe('useStickToBottom · 贴底四件套', () => {
  it('距离阈值 48px 判定：贴底 follow 直接回底（scrollTop = scrollHeight）', () => {
    const geometry: FakeScroll = { scrollTop: 560, scrollHeight: 1000, clientHeight: 400 };
    const { read, el } = renderHost(geometry); // 距底 40 ≤ 48
    act(() => read().follow('k1'));
    expect(el.scrollTop).toBe(1000);
    expect(read().atBottom).toBe(true);
    expect(read().unreadCount).toBe(0);
  });

  it('先记录贴底态再追加：脱离后新内容不拉视口，未读计入 FAB（N）', () => {
    const geometry: FakeScroll = { scrollTop: 0, scrollHeight: 2000, clientHeight: 400 };
    const { read } = renderHost(geometry);

    // 用户上翻：距底 1600 > 48
    act(() => read().onScroll());
    expect(read().atBottom).toBe(false);

    const viewportBefore = geometry.scrollTop;
    act(() => read().follow('k2'));
    expect(geometry.scrollTop).toBe(viewportBefore); // 视口不动（缓冲照常追加）
    expect(read().unreadCount).toBe(1);
    act(() => read().follow('k3'));
    expect(read().unreadCount).toBe(2);
    // 同一变化 key 不重复计数（幂等）
    act(() => read().follow('k3'));
    expect(read().unreadCount).toBe(2);
  });

  it('回底：scrollToBottom 清零未读并恢复跟随', () => {
    const geometry: FakeScroll = { scrollTop: 0, scrollHeight: 2000, clientHeight: 400 };
    const { read } = renderHost(geometry);
    act(() => read().onScroll());
    expect(read().atBottom).toBe(false);
    act(() => read().follow('k2'));
    expect(read().unreadCount).toBe(1);

    act(() => read().scrollToBottom());
    expect(read().atBottom).toBe(true);
    expect(read().unreadCount).toBe(0);

    // 回底后 follow 自动跟随
    geometry.scrollHeight = 2400;
    act(() => read().follow('k4'));
    expect(geometry.scrollTop).toBe(2400);
  });

  it('阈值边界：恰好 48px 视为贴底（含），超过则脱离', () => {
    const geometry: FakeScroll = { scrollTop: 552, scrollHeight: 1000, clientHeight: 400 };
    const { read } = renderHost(geometry);
    // 距底 = 1000 - 552 - 400 = 48 → 贴底
    expect(1000 - geometry.scrollTop - geometry.clientHeight).toBe(STICK_THRESHOLD_PX);
    act(() => read().onScroll());
    expect(read().atBottom).toBe(true);

    geometry.scrollTop = 551; // 距底 49 → 脱离
    act(() => read().onScroll());
    expect(read().atBottom).toBe(false);
  });
});
