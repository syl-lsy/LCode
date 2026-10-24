import { createBrowserRouter, RouterProvider } from 'react-router';

import App, { HomeView, SessionView } from './App';
import { LoginView } from '@/features/auth/LoginView';
import { OnboardingView } from '@/features/onboarding/OnboardingView';

/**
 * 路由（A4 §2.1 router.tsx）：/ → 空态首页（问候 + Composer）；/s/:sessionId →
 * 会话视图（深链刷新恢复路由态，任务 #2 交付判据）。
 * /login、/onboarding（B7）：登录/欢迎与新手引导纯静态占位页——LCode 鉴权
 * P2 前不实现、首页不强制跳转引导，两页独立全屏（不入 AppShell 三栏），
 * 直接 URL 可达。
 *
 * `routes` 同时供 createBrowserRouter（生产）与 createMemoryRouter（组件测试，
 * tests/test-utils.tsx）使用——同一份路由结构，避免测试双事实源。
 */

export const routes = [
  { path: '/login', element: <LoginView /> },
  { path: '/onboarding', element: <OnboardingView /> },
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
