import js from '@eslint/js';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
import tseslint from 'typescript-eslint';

/**
 * ESLint flat config（A4 §2.1）。
 *
 * 依赖方向门禁（A4 §2.2）用 no-restricted-imports 强制。执法策略（M1 审查 P2 修复，
 * 选型 ii「最小改动」，理由见当日报告）：
 * - 禁令同时覆盖三种等价路径形态：'@/…' 别名、相对上跳（'../F'、'../../F'）、
 *   Vite 根绝对 '/src/…'——三者指向同一模块，只封别名等于没封；
 * - feature 名集合 FEATURES 封闭（A4 §2.1 七个），相对路径按 A4 规定的 feature
 *   内部三段式（feature 根 / components/ / hooks/）枚举一跳与两跳；
 * - chat 容器例外收窄到 features/chat/components/**（消息流容器引 approval/todo
 *   卡片）；chat 的 store.ts / hooks / Composer.tsx 不享受例外；
 * - workbenchStore 是「UI 编排 store」：其 action 允许被跨 feature 调用（A4 §2.2
 *   跨 feature 通信规则），故跨 feature 禁令对 workbench/store 单点以 '!' 反忽略。
 *
 * 剩余边界（已知局限，M2 重估点）：
 * - 深于两跳的相对绕行（如 '../../../features/…'）未枚举——A4 规定 features 内部
 *   三段式、不存在更深结构；若 M2 出现更深嵌套或需要正向白名单，改用
 *   eslint-plugin-boundaries 正向分层声明（审查备选一）；
 * - events 写各 feature store 的「正向白名单」无法用 import 模式表达，当前 events
 *   块对 features 全禁属过严口径——M2 任务 4 dispatcher 需要 import 各 feature
 *   store 时，把该块放宽为「仅禁 features/<F>/components 与 @/components」。
 *
 * 块序注意：flat config 同一文件命中多个块时，同名规则「后者整体覆盖前者」
 * （不合并 options）。features/<F>/store.ts 同时命中 feature 块与 store 块，
 * 故 store 块放在 feature 块之后，且自带跨 feature 禁令全集（并集语义手工保证）。
 */

/** feature 名封闭集合（A4 §2.1 七个；costs/todo 目录 M2/M3 落地，门禁先行铺好）。 */
const FEATURES = ['approval', 'chat', 'costs', 'models', 'sessions', 'todo', 'workbench'];

/**
 * 生成「跨 feature import」禁令 patterns：别名 + 一跳相对 + 两跳相对三形态。
 * @param {string[]} allowed 放行的 feature 名（chat 容器例外 = ['approval', 'todo']）
 */
function crossFeaturePatterns(allowed = []) {
  const patterns = [];
  for (const name of FEATURES) {
    if (allowed.includes(name)) continue;
    for (const base of [`@/features/${name}`, `../${name}`, `../../${name}`]) {
      if (name === 'workbench') {
        // workbenchStore 是 UI 编排 store：action 允许跨 feature 调用（A4 §2.2），
        // 仅以 '!' 反忽略 store 单模块（组件仍禁入消息流）。
        // 注意：不能推入 bare 形态 '@/features/workbench'——gitignore 语义下 bare
        // 模式命中目录本身，触发「父目录被排除后不可再包含」规则，'!…/store'
        // 反忽略会失效（Linter API 实测确认）。仅 /** + !store 组合有效。
        patterns.push(`${base}/**`, `!${base}/store`);
      } else {
        patterns.push(base, `${base}/**`);
      }
    }
  }
  return patterns;
}

const CROSS_FEATURE_MESSAGE =
  '禁止跨 feature import（A4 §2.2；唯一例外：chat 的 components/ 可引 approval/todo 卡片，workbench 不进流）；feature 内部用相对路径';

/**
 * Vite 根绝对路径 import（/src/…）与别名指向同一模块，统一禁止，防绕过路径门禁。
 * 形态说明（Linter API 实测）：'/src/**' 与 'src/**' 均匹配不到带前导斜杠的
 * import 源串（gitignore 锚定语义），'**\/src/**' 生效且对 @contracts/相对路径
 * 等合法形态零误伤。
 */
const ROOT_ABSOLUTE_BAN = {
  group: ['**/src/**'],
  message: '禁止根绝对路径 import（/src/…）：统一走 @/ 别名，防绕过依赖方向门禁',
};

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
                '../../features/**',
                '../../stores/**',
                '../../api/**',
                '../../events/**',
              ],
              message:
                'components/ 为纯展示层：禁止依赖 features/stores/api/events（A4 §2.2 依赖方向）',
            },
            ROOT_ABSOLUTE_BAN,
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
            { group: crossFeaturePatterns(), message: CROSS_FEATURE_MESSAGE },
            ROOT_ABSOLUTE_BAN,
          ],
        },
      ],
    },
  },
  {
    // chat 非组件层（store.ts / hooks/ / Composer.tsx）：容器例外不适用（审查 P2-b）
    files: ['src/features/chat/**/*.{ts,tsx}'],
    ignores: ['src/features/chat/components/**'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              group: crossFeaturePatterns(),
              message: `${CROSS_FEATURE_MESSAGE}；chat 的容器例外仅限 components/，store/hooks/Composer 不享受`,
            },
            ROOT_ABSOLUTE_BAN,
          ],
        },
      ],
    },
  },
  {
    // chat components = 消息流容器：仅放行 approval/todo 卡片；其余 feature（含
    // workbench 组件）禁入（A4 §2.2 例外条款的字面范围）
    files: ['src/features/chat/components/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            { group: crossFeaturePatterns(['approval', 'todo']), message: CROSS_FEATURE_MESSAGE },
            ROOT_ABSOLUTE_BAN,
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
                '../../features/**',
                '../../stores/**',
                '../../events/**',
              ],
              message:
                'api/ 为最底层：仅依赖 @contracts（与 lib/），禁止依赖 features/stores/events（A4 §2.2）',
            },
            ROOT_ABSOLUTE_BAN,
          ],
        },
      ],
    },
  },
  {
    // store = 纯 TS 模块：禁 React 与组件路径（含 './components'、'../components'
    // 相对绕行，审查 P2-c）+ 跨 feature 严格禁令（chat store 不享受卡片例外，P2-b）。
    // 本块必须位于 feature 块之后：flat config 后者覆盖前者，patterns 取并集。
    files: ['src/stores/**/*.ts', 'src/features/**/store.ts'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              // feature 域的禁令（含 workbench/store 反忽略例外）由下方
              // crossFeaturePatterns 组承担——'@/features/**' 平铺禁令会因
              // 「父目录排除不可再包含」规则使例外失效，故此处不重复设置。
              group: [
                'react',
                'react-dom',
                '@/components/**',
                './components/**',
                '../components/**',
                '../../components/**',
              ],
              message:
                'store 为纯 TS 模块：禁止依赖 React 组件（A4 §2.2）；infra store（src/stores）禁 features，feature store 内部一律相对路径',
            },
            { group: crossFeaturePatterns(), message: CROSS_FEATURE_MESSAGE },
            ROOT_ABSOLUTE_BAN,
          ],
        },
      ],
    },
  },
  {
    // events：过严口径见文件头「剩余边界」第二条（M2 任务 4 放宽为仅禁 components）
    files: ['src/events/**/*.ts'],
    ignores: ['src/events/**/*.test.ts'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              group: [
                '@/features/**',
                '@/components/**',
                '../features/**',
                '../components/**',
                '../../features/**',
                '../../components/**',
              ],
              message:
                'events/ 不 import React 组件（A4 §2.2）；dispatcher 对 feature store 的白名单 M2 任务 4 落地时放宽（见 eslint.config.js 文件头）',
            },
            ROOT_ABSOLUTE_BAN,
          ],
        },
      ],
    },
  },
);
