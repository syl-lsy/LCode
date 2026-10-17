import { fileURLToPath } from 'node:url';

import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

/** 仓库根 = web/ 的上一级（contracts/ 生成物与 fixtures 在 web/ 之外）。 */
const repoRoot = fileURLToPath(new URL('..', import.meta.url));
const resolve = (p: string): string => fileURLToPath(new URL(p, import.meta.url));

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      // 契约 fixtures 别名必须先于 @contracts 声明（前缀更长者优先，防误匹配）
      '@contracts-fixtures': resolve('../contracts/tests/fixtures'),
      '@contracts': resolve('../contracts/generated/ts'),
      '@': resolve('./src'),
    },
    dedupe: ['react', 'react-dom', 'zod'],
  },
  server: {
    // A4 §7.4：dev 同源代理，与生产（反代同源）零差异，免 CORS
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
    fs: {
      // @contracts 生成物/fixtures 位于仓库根下（web/ 之外），dev server 需放行
      allow: [repoRoot],
    },
  },
  test: {
    // 骨架期仅纯 TS 冒烟测试（node 环境即可，A4 §10.2）；
    // M2 引入组件测试后按需切 jsdom + @testing-library。
    environment: 'node',
    include: ['src/**/*.test.{ts,tsx}'],
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/**/*.test.{ts,tsx}', 'src/tests/**', 'src/main.tsx'],
      // A4 §10.5 初始口径：全局 80/70、src/events ≥90。
      // 骨架期业务代码尚无测试，test:coverage 预期红——门禁命令是 `test`（vitest run），
      // 覆盖率达标随 M2 各任务的测试补齐兑现。
      thresholds: {
        lines: 80,
        statements: 80,
        functions: 80,
        branches: 70,
        'src/events/**': {
          lines: 90,
          statements: 90,
          functions: 90,
          branches: 90,
        },
      },
    },
  },
});
