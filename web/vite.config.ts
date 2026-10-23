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
    port: 8080,
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
      // M2 收口台账 G1（2026-10-19 定档）：阈值挂进门禁命令 `make test-web`
      // （= vitest run --coverage，经 scripts/vitest-run.mjs 透传）。
      // 基线（当日 `pnpm --dir web test:coverage` 实测，All files）：
      //   statements 93.39 / branches 86.14 / functions 94.69 / lines 95.39
      // 阈值取基线之下、留 4~6 点回退余量；上调前须先复测基线。
      // src/events（事件管线核心）沿用 A4 §10.5 的 ≥90 单列口径（基线
      // 97.26/90.88/97.69/99.48，branches 90 为紧约束，退化即红）。
      thresholds: {
        lines: 90,
        statements: 85,
        functions: 85,
        branches: 80,
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
