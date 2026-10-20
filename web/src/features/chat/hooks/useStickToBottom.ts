import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * 贴底四件套（A4 §6.4；streaming-buffer 规则 19/20）：
 *
 * ① 距离阈值 48px 判定贴底；
 * ② 消息流高度变化时（流式追加/卡片展开/历史插入）先读当前贴底态，再决定是否回底
 *    ——用户上翻 = 视口不动、缓冲照常追加；
 * ③ 脱离后显示「回到底部 ↓N」FAB（N = 脱离期间新到达的条目数），回底后清零；
 * ④ 组件内禁止直接 scrollTo——所有高度变化经 `follow()` 这唯一滚动入口。
 */

/** 贴底判定距离阈值（px，§6.4）。 */
export const STICK_THRESHOLD_PX = 48;

export interface StickToBottom {
  /** 滚动容器 ref（调用方挂到 overflow-y-auto 元素上） */
  containerRef: React.RefObject<HTMLDivElement | null>;
  /** 容器 onScroll 绑定（用户滚动时更新贴底态） */
  onScroll: () => void;
  /** 当前是否贴底（FAB 显隐） */
  atBottom: boolean;
  /** 脱离期间未读条目数（FAB ↓N） */
  unreadCount: number;
  /** 高度变化后的唯一滚动入口：贴底则回底；脱离则累计未读 */
  follow: (changeKey?: unknown) => void;
  /** 手动回底（FAB 点击）：滚到底并清零未读 */
  scrollToBottom: () => void;
}

function distanceFromBottom(el: HTMLElement): number {
  return el.scrollHeight - el.scrollTop - el.clientHeight;
}

export function useStickToBottom(): StickToBottom {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [atBottom, setAtBottom] = useState(true);
  const [unreadCount, setUnreadCount] = useState(0);
  const atBottomRef = useRef(true);
  /** follow 去重锚：同一次高度变化（同一 changeKey）只结算一次 */
  const lastKeyRef = useRef<unknown>(undefined);

  const recompute = useCallback((): boolean => {
    const el = containerRef.current;
    if (el === null) return atBottomRef.current;
    const near = distanceFromBottom(el) <= STICK_THRESHOLD_PX;
    if (near !== atBottomRef.current) {
      atBottomRef.current = near;
      if (near) setUnreadCount(0); // 回底即清零未读
      setAtBottom(near);
    }
    return near;
  }, []);

  const onScroll = useCallback((): void => {
    recompute();
  }, [recompute]);

  const scrollToBottom = useCallback((): void => {
    const el = containerRef.current;
    if (el === null) return;
    el.scrollTop = el.scrollHeight;
    atBottomRef.current = true;
    setUnreadCount(0);
    setAtBottom(true);
  }, []);

  const follow = useCallback(
    (changeKey?: unknown): void => {
      if (changeKey !== undefined) {
        if (Object.is(lastKeyRef.current, changeKey)) return; // 同一变化不重复结算
        lastKeyRef.current = changeKey;
      }
      // ② 先记录是否贴底（当前滚动位置未被程序改动），再结算
      const near = recompute();
      if (near) {
        const el = containerRef.current;
        if (el !== null) el.scrollTop = el.scrollHeight; // 回底跟随
        return;
      }
      // ③ 脱离中：新内容到达 → 未读 +1（按 changeKey 去重后的每次真实变化）
      if (changeKey !== undefined) setUnreadCount((n) => n + 1);
    },
    [recompute],
  );

  return { containerRef, onScroll, atBottom, unreadCount, follow, scrollToBottom };
}

/** 便捷组合：内容 key（条目数/版本/generation）变化时自动 follow 一次。 */
export function useAutoFollow(stick: StickToBottom, changeKey: unknown): void {
  const keyRef = useRef<unknown>(changeKey);
  useEffect(() => {
    if (Object.is(keyRef.current, changeKey)) return;
    keyRef.current = changeKey;
    stick.follow(changeKey);
    // stick 引用稳定（hook 内全部 useCallback），只依赖 changeKey
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [changeKey]);
}
