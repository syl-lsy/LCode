// `pnpm test [args]` 的透传薄包装 = `vitest run [args]`。
//
// 为什么需要这层包装（M2 收口台账 G1，2026-10-19 实测）：
// pnpm 10 会把 `pnpm test -- --coverage` 里的 `--` **原样**传进脚本 argv，
// 而 vitest 5.0.4 遇到裸 `--` 之后会静默忽略全部后续 CLI 选项——实测
// `pnpm --dir web test -- --coverage` 根本不启用 coverage、退出码 0，
// 负控命令（--coverage.thresholds.lines=100 必须红）无从生效。
// 包装层剥掉一个前导 `--` 再调 vitest，使 `pnpm test [-- <vitest 选项>]`
// 的命令形态（Makefile test-web 门禁与 CI 负控）语义确定。
// 不带任何参数的裸 `pnpm test`（test-web-fast / run_changed_tests）行为不变。
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const args = process.argv.slice(2);
if (args[0] === '--') {
  args.shift();
}

const vitestBin = fileURLToPath(new URL('../node_modules/.bin/vitest', import.meta.url));
const result = spawnSync(vitestBin, ['run', ...args], { stdio: 'inherit', cwd: process.cwd() });

if (result.error) {
  console.error(`[vitest-run] 启动 vitest 失败：${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
