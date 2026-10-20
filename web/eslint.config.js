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
 * - events 块已按 M2 任务 4 落地为「正向白名单」：仅放行 features/<F>/store
 *   （dispatcher/handlers 是唯一反向写入口，A4 §2.2/§5），feature 组件与
 *   components/ 仍禁。见文件尾 events 块与 eventsFeatureStorePatterns()。
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
 * Vite 根绝对路径 import（/src/…）与别名指向同一模块，统一禁止，防绕过依赖方向门禁。
 * 形态说明（Linter API 实测）：'/src/**' 与 'src/**' 均匹配不到带前导斜杠的
 * import 源串（gitignore 锚定语义），'**\/src/**' 生效且对 @contracts/相对路径
 * 等合法形态零误伤。
 */
const ROOT_ABSOLUTE_BAN = {
  group: ['**/src/**'],
  message: '禁止根绝对路径 import（/src/…）：统一走 @/ 别名，防绕过依赖方向门禁',
};

/** events 块的白名单提示语（store 之外一律禁，含 feature 组件与全局 components/）。 */
const EVENTS_BAN_MESSAGE =
  'events/ 只许 import features/<F>/store（dispatcher 是各 feature store 的唯一反向写入口，A4 §2.2/§5）；feature 组件与 components/ 一律禁止';

/**
 * events → feature store 白名单 patterns（三种等价路径形态 × 七 feature）：
 * `<base>/**` 先排除整个 feature 目录，`!<base>/store` 反忽略 store 单模块。
 * 反忽略用 '/store' 精确路径而非 bare 目录（gitignore「父目录排除后不可再包含」
 * 语义，见文件头 workbenchStore 例外注释的实测记录）。
 */
function eventsFeatureStorePatterns() {
  const patterns = [];
  for (const name of FEATURES) {
    for (const base of [`@/features/${name}`, `../features/${name}`, `../../features/${name}`]) {
      patterns.push(`${base}/**`, `!${base}/store`);
    }
  }
  return patterns;
}

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
    // Node 工具层：构建/测试配置 + scripts/（G1 的 vitest-run.mjs 等纯 Node 脚本）
    files: ['vite.config.ts', 'eslint.config.js', 'scripts/**/*.mjs'],
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
    // events（A4 §2.2 正向白名单口径，M2 任务 4 落地）：dispatcher/handlers 是各 feature
    // store 的唯一反向写入口 → 放行 `features/<F>/store`（含 workbench 的三 store 单模块）；
    // feature 组件（components/**、hooks/** 等）与全局 components/ 仍一律禁止。
    // 实现沿用 workbenchStore 例外的实测组合：`<base>/**` 先排除、`!<base>/store` 反忽略
    // （bare 形态受「父目录排除后不可再包含」限制，见文件头注释）。
    files: ['src/events/**/*.ts'],
    ignores: ['src/events/**/*.test.ts'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              group: [
                ...eventsFeatureStorePatterns(),
                '@/components/**',
                '../components/**',
                '../../components/**',
              ],
              message: EVENTS_BAN_MESSAGE,
            },
            ROOT_ABSOLUTE_BAN,
          ],
        },
      ],
    },
  },
);
