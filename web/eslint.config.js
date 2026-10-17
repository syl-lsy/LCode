import js from '@eslint/js';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
import tseslint from 'typescript-eslint';

/**
 * ESLint flat config（A4 §2.1）。
 *
 * 依赖方向门禁（A4 §2.2）用 no-restricted-imports 强制：
 * - components/        → 禁 features/stores/api/events（纯展示：props 进、回调出）
 * - features/X         → 禁跨 feature import（唯一例外 chat 容器，且 workbench 不进流）
 * - api/               → 禁 features/stores/events（最底层，仅依赖 @contracts）
 * - store（含 feature 内 store.ts）→ 禁 React（zustand store 为纯 TS 模块）
 * - events/            → 禁 features/components（dispatcher 经 store 写入，唯一反向写入口）
 *
 * 说明：限制以 '@/…' 别名与跨目录相对路径为主；events 写 store 的白名单
 * （"可依赖各 feature 的 store"）无法用 import 模式表达正向清单，M2 dispatcher
 * 落地时如出现绕行再收紧（如自定义 rule 或 boundaries 插件）。
 */
export default tseslint.config(
  { ignores: ['dist/', 'coverage/', 'node_modules/'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      globals: { ...globals.browser, ...globals.es2022 },
    },
    plugins: { 'react-hooks': reactHooks },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
    },
  },
  {
    files: ['vite.config.ts', 'eslint.config.js'],
    languageOptions: {
      globals: { ...globals.node },
    },
  },

  // ---- 依赖方向门禁（A4 §2.2） ----
  {
    files: ['src/components/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              group: [
                '@/features/**',
                '@/stores/**',
                '@/api/**',
                '@/events/**',
                '../features/**',
                '../stores/**',
                '../api/**',
                '../events/**',
              ],
              message:
                'components/ 为纯展示层：禁止依赖 features/stores/api/events（A4 §2.2 依赖方向）',
            },
          ],
        },
      ],
    },
  },
  {
    files: ['src/features/**/*.{ts,tsx}'],
    ignores: ['src/features/chat/**'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              group: ['@/features/**'],
              message:
                '禁止跨 feature import（A4 §2.2，唯一例外为 chat 容器）；feature 内部请用相对路径',
            },
          ],
        },
      ],
    },
  },
  {
    files: ['src/features/chat/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              group: ['@/features/workbench/**'],
              message:
                'chat 容器例外仅限消息流卡片（approval/todo 等）；workbench 不进流（A4 §2.2）',
            },
          ],
        },
      ],
    },
  },
  {
    files: ['src/api/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              group: [
                '@/features/**',
                '@/stores/**',
                '@/events/**',
                '../features/**',
                '../stores/**',
                '../events/**',
              ],
              message:
                'api/ 为最底层：仅依赖 @contracts（与 lib/），禁止依赖 features/stores/events（A4 §2.2）',
            },
          ],
        },
      ],
    },
  },
  {
    files: ['src/stores/**/*.ts', 'src/features/**/store.ts'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              group: ['react', 'react-dom', '@/components/**', '@/features/**'],
              message: 'store 为纯 TS 模块：禁止依赖 React 组件（A4 §2.2）',
            },
          ],
        },
      ],
    },
  },
  {
    files: ['src/events/**/*.ts'],
    ignores: ['src/events/**/*.test.ts'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              group: ['@/features/**', '@/components/**', '../features/**', '../components/**'],
              message: 'events/ 只经 store 写入，不 import React 组件（A4 §2.2）',
            },
          ],
        },
      ],
    },
  },
);
