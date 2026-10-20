import { createBrowserRouter, RouterProvider } from 'react-router';

import App, { HomeView, SessionView } from './App';

/**
 * 路由（A4 §2.1 router.tsx）：/ → 空态首页（问候 + Composer）；/s/:sessionId →
 * 会话视图（深链刷新恢复路由态，任务 #2 交付判据）。其余 P1 占位路由不建。
 *
 * `routes` 同时供 createBrowserRouter（生产）与 createMemoryRouter（组件测试，
 * tests/test-utils.tsx）使用——同一份路由结构，避免测试双事实源。
 */

export const routes = [
  {
    path: '/',
    element: <App />,
    children: [
      { index: true, element: <HomeView /> },
      { path: 's/:sessionId', element: <SessionView /> },
    ],
  },
];

export const router = createBrowserRouter(routes);

/** 应用路由根（main.tsx 挂载）。 */
export function RouterRoot() {
  return <RouterProvider router={router} />;
}
