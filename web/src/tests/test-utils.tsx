// 本文件仅被 jsdom 环境的测试文件 import（组件/回放测试）：
// 它在 import 期注册 MSW 生命周期钩子 + 每测 store 复位，见文件尾。
/**
 * 测试基建（A4 §10.1 test-utils 行 / §10.3）：
 * - renderWithProviders：以 routes（生产同一份路由结构）挂 createMemoryRouter，
 *   支持初始路由（深链 /s/:id）；
 * - resetAllStores：全部 store 复位 + dispatcher 水位/缓冲复位 + localStorage 清空
 *   （测试隔离；store 单例跨用例共享，不复位会串场）；
 * - MSW server：默认 handlers（msw-handlers.ts），onUnhandledRequest: 'error'
 *   （A4 §10.1——漏 mock 的请求直接红，防静默假通过）。
 */

import '@testing-library/jest-dom/vitest';

import { cleanup, render } from '@testing-library/react';
import { setupServer } from 'msw/node';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, beforeAll, afterAll, beforeEach, expect } from 'vitest';
import type { ReactElement } from 'react';

import { routes } from '@/router';

import { useApprovalStore } from '@/features/approval/store';
import { useChatStore } from '@/features/chat/store';
import { useCostStore } from '@/features/costs/store';
import { useModelsStore } from '@/features/models/store';
import { useSessionStore } from '@/features/sessions/store';
import { useTodoStore } from '@/features/todo/store';
import { useDiffStore, useTerminalStore, useWorkbenchStore } from '@/features/workbench/store';

import { deltaBuffers, terminalBuffer } from '@/events/buffers';
import { resetDispatcher } from '@/events/dispatcher';

import { useConnStore } from '@/stores/connStore';

import { handlers } from './msw-handlers';

/** MSW server（A4 §10.1）：默认 handlers + 未 mock 请求即红。 */
export const server = setupServer(...handlers);

// msw v3：SharedOptions 的开关名为 onUnhandledFrame（v2 是 onUnhandledRequest）
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterEach(() => {
  server.resetHandlers();
  cleanup();
});
afterAll(() => server.close());
beforeEach(() => {
  resetAllStores();
});

/** 全部 store + 事件管线复位（每测前自动执行；特殊场景可手动再调）。 */
export function resetAllStores(): void {
  // localStorage 先清：workbench/ui store 的初始值会读取持久化键，顺序反了会把
  // 上一用例的 width/collapsed/theme 带回来
  localStorage.clear();
  useChatStore.getState().reset();
  useSessionStore.getState().reset();
  useModelsStore.getState().reset();
  useCostStore.getState().reset();
  useTodoStore.getState().reset();
  useApprovalStore.getState().reset();
  useTerminalStore.getState().reset();
  useDiffStore.getState().reset();
  useWorkbenchStore.getState().reset();
  useConnStore.getState().reset();
  resetDispatcher();
  deltaBuffers.clear();
  terminalBuffer.clear();
  document.documentElement.className = '';
  document.documentElement.style.cssText = '';
}

export interface RenderAtRouteResult {
  /** 内存路由实例（可 router.navigate 驱动路由断言） */
  router: ReturnType<typeof createMemoryRouter>;
}

/** 以生产同一份 routes 挂内存路由（初始路由 = 深链场景）。 */
export function renderAtRoute(route: string): RenderAtRouteResult {
  const router = createMemoryRouter(
    routes.map((routeEntry) => ({ ...routeEntry })),
    { initialEntries: [route] },
  );
  render(<RouterProvider router={router} />);
  return { router };
}

/** 无路由组件的普通渲染（需要 router 的组件自行包 MemoryRouter/内存路由）。 */
export function renderUi(ui: ReactElement): ReturnType<typeof render> {
  return render(ui);
}

/** 断言辅助：vitest-axe 挂载（交互组件 a11y 冒烟用，A4 §10.1）。 */
export async function expectNoAxeViolations(container: HTMLElement): Promise<void> {
  const [{ axe }, { toHaveNoViolations }] = await Promise.all([
    import('vitest-axe'),
    import('vitest-axe/dist/matchers.js'),
  ]);
  expect.extend({ toHaveNoViolations } as never);
  const results = await axe(container);
  (expect(results) as unknown as { toHaveNoViolations: () => void }).toHaveNoViolations();
}
