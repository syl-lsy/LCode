#!/usr/bin/env node
/**
 * fixtures ↔ zod 契约校验（zod 侧的实际严格度实测面，并入 tools/ts-check.sh）。
 *
 * 校验面：
 * - events/{valid,invalid}/**.json —— 一律过 EventUnionSchema（前端 dispatcher 的
 *   实际校验面：12 型判别联合；valid 必须接受，invalid 必须拒绝）；
 * - rest/、findings/ 的 fixture 按显式映射表过对应 DTO/schema（与 Python 侧
 *   tests/test_rest.py / test_findings.py 的映射同构；映射缺漏 = 死文件，报 FAIL）。
 *
 * 豁免清单（INVALID_EXEMPTIONS）：invalid fixture 被 zod 接受是允许的，但必须逐条
 * 显式列明原因——只允许是"JSON Schema 不可表达"的 Python 侧 model_validator 跨字段
 * 规则。反向也守门：豁免条目若实际已被 zod 拒绝 = 清单过期，报 FAIL（防豁免腐烂）。
 * 不许静默放过。
 *
 * 运行前提：generated/ts 已按当前单源生成（ts-check.sh 中 `node gen_ts.mjs` 先行）。
 */
import { registerHooks } from "node:module";
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const TS_DIR = path.resolve(here, "../../generated/ts");
const FIXTURES_DIR = path.resolve(here, "../../tests/fixtures");

// 生成物内的 `import { z } from "zod"` 相对 generated/ 解析不到 node_modules
// （工具链 node_modules 在 tools/ts/）——用解析钩子把 "zod" 映射到工具链自身的解析结果。
const zodUrl = import.meta.resolve("zod");
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "zod") return { url: zodUrl, shortCircuit: true };
    return nextResolve(specifier, context);
  },
});

function pascal(stem) {
  return stem
    .split(/[-_]/)
    .map((s) => s.charAt(0).toUpperCase() + s.slice(1))
    .join("");
}

/** 载入 generated/ts/<rel>.zod.ts，返回其导出的主 schema（导出名 = <Pascal(文件名)>Schema）。 */
const schemaCache = new Map();
async function loadSchema(rel) {
  if (schemaCache.has(rel)) return schemaCache.get(rel);
  const file = path.join(TS_DIR, `${rel}.zod.ts`);
  let mod;
  try {
    mod = await import(pathToFileURL(file).href);
  } catch (err) {
    throw new Error(`载入 ${rel}.zod.ts 失败（生成物缺失或语法错误）：${err.message}`);
  }
  const exportName = `${pascal(path.basename(rel))}Schema`;
  const schema = mod[exportName];
  if (schema === undefined) {
    throw new Error(`${rel}.zod.ts 未导出 ${exportName}（实际导出：${Object.keys(mod).join(", ")}）`);
  }
  schemaCache.set(rel, schema);
  return schema;
}

async function listFixtureRels(sub) {
  const out = [];
  async function walk(dir, prefix) {
    let entries;
    try {
      entries = await readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of entries) {
      const rel = prefix ? `${prefix}/${e.name}` : e.name;
      if (e.isDirectory()) await walk(path.join(dir, e.name), rel);
      else if (e.name.endsWith(".json")) out.push(rel);
    }
  }
  await walk(path.join(FIXTURES_DIR, sub), sub);
  return out.sort();
}

// ---------------------------------------------------------------- 映射表

/** rest fixture -> zod schema 相对路径（与 tests/test_rest.py 的两个映射表同构） */
const REST_FIXTURE_SCHEMAS = {
  "rest/valid/message_send_ack.json": "rest/message_send_ack",
  "rest/valid/approval_decision_edit.json": "rest/approval_decision_request",
  "rest/valid/session_detail.json": "rest/session_detail",
  "rest/valid/cost_summary.json": "rest/cost_summary",
  "rest/valid/approval_item_auto.json": "rest/approval_item",
  "rest/invalid/message_create__blank.json": "rest/message_create",
  "rest/invalid/session_patch__empty.json": "rest/session_patch",
  "rest/invalid/approval_decision__bad_decision.json": "rest/approval_decision_request",
  "rest/invalid/approval_decision__respond_without_feedback.json": "rest/approval_decision_request",
};

/** findings fixture -> zod schema 相对路径 */
const FINDINGS_FIXTURE_SCHEMAS = {
  "findings/valid/reviewer_findings.json": "structured/reviewer_findings",
  "findings/invalid/reviewer_findings__bad_severity.json": "structured/reviewer_findings",
  "findings/invalid/reviewer_findings__end_line_before_line.json": "structured/reviewer_findings",
  "findings/invalid/reviewer_findings__end_line_without_line.json": "structured/reviewer_findings",
  "findings/invalid/reviewer_findings__line_zero.json": "structured/reviewer_findings",
};

/**
 * invalid fixture 豁免清单：被 zod 接受且**仅因** Python 侧跨字段 model_validator
 * （JSON Schema 无法表达）。每条必须注明契约侧 validator 出处。
 */
const INVALID_EXEMPTIONS = {
  "events/invalid/approval_resolved__edit_without_edited_args.json":
    "ApprovalResolvedEvent._check_decision_payload：decision=edit ⇒ edited_args 交叉必填",
  "events/invalid/diff_ready__totals_mismatch.json":
    "DiffReadyEvent._check_totals：totals 必须等于逐文件求和（跨字段算术）",
  "events/invalid/message_finished__structured_ok_without_data.json":
    "StructuredResult._check_data：status=ok ⇒ data 非空（交叉必填）",
  "events/invalid/run_status__compact_finished_missing_info.json":
    "RunStatusEvent._check_subject_payload：subject=compact, phase=finished ⇒ compact 必填",
  "events/invalid/run_status__compact_stopped_phase.json":
    "RunStatusEvent._check_subject_payload：subject=compact 不存在 stopped 相位（枚举×subject 交叉）",
  "events/invalid/run_status__run_error_missing_error.json":
    "RunStatusEvent._check_subject_payload：subject=run, phase=error ⇒ error 必填",
  "events/invalid/sandbox_output__lifecycle_missing_phase.json":
    "SandboxOutputEvent._check_channel：channel=lifecycle ⇒ phase 必填",
  "events/invalid/sandbox_output__stdout_missing_exec_id.json":
    "SandboxOutputEvent._check_channel：channel=stdout/stderr ⇒ exec_id 必填",
  "events/invalid/sandbox_output__stream_with_phase.json":
    "SandboxOutputEvent._check_channel：phase 仅允许 channel=lifecycle（channel×phase 交叉）",
  "events/invalid/tool_call_finished__error_missing_error_obj.json":
    "ToolCallFinishedEvent._check_error_status：status=error ⇒ error 必填",
  "events/invalid/tool_call_finished__success_with_error.json":
    "ToolCallFinishedEvent._check_error_status：status=success ⇒ 不得携带 error",
  "rest/invalid/message_create__blank.json":
    "MessageCreate._non_blank：content 全空白拒绝（strip 语义，JSON Schema 不可表达）",
  "rest/invalid/session_patch__empty.json":
    "SessionPatch._at_least_one：至少提供一个更新字段（字段集合存在性约束）",
  "rest/invalid/approval_decision__respond_without_feedback.json":
    "ApprovalDecisionRequest._check_payload：decision=respond ⇒ feedback 非空",
  "findings/invalid/reviewer_findings__end_line_before_line.json":
    "Finding._check_line_window：end_line ≥ line（跨字段比较）",
  "findings/invalid/reviewer_findings__end_line_without_line.json":
    "Finding._check_line_window：end_line 与 line 成对出现",
};

// ---------------------------------------------------------------- 执行

const failures = [];
const stats = { validAccepted: 0, validTotal: 0, invalidRejected: 0, invalidTotal: 0, exempted: 0 };

function checkFixture(rel, schema, expectValid) {
  const data = JSON.parse(fixtureCache.get(rel));
  const result = schema.safeParse(data);
  if (expectValid) {
    stats.validTotal += 1;
    if (result.success) stats.validAccepted += 1;
    else {
      const issue = result.error.issues[0];
      failures.push(
        `valid fixture 被拒：${rel}（第一条 issue：${issue.path.join(".") || "(root)"} ${issue.message}）`,
      );
    }
    return;
  }
  stats.invalidTotal += 1;
  const exempt = Object.hasOwn(INVALID_EXEMPTIONS, rel);
  if (!result.success) {
    if (exempt) failures.push(`豁免过期：${rel} 已被 zod 拒绝，应从 INVALID_EXEMPTIONS 移除`);
    else stats.invalidRejected += 1;
  } else if (exempt) {
    stats.exempted += 1;
  } else {
    failures.push(`invalid fixture 未被 zod 拒绝且无豁免：${rel}（zod 侧假绿，须修复生成器或补豁免）`);
  }
}

const fixtureCache = new Map();
async function preload(rel) {
  fixtureCache.set(rel, await readFile(path.join(FIXTURES_DIR, rel), "utf8"));
}

async function main() {
  // 1) 前端 dispatcher 的实际校验面：判别联合（vacuity 守卫 + 全部事件 fixture 过它）
  const union = await loadSchema("events/event_union");
  if (union.safeParse({}).success) {
    failures.push("EventUnionSchema 接受空对象（z.any() 退化回归，vacuity 守卫失败）");
  }

  // 2) 接线完整性：rest/findings 在盘 fixture 必须全部有映射（防死文件）
  for (const [dirName, table] of [
    ["rest", REST_FIXTURE_SCHEMAS],
    ["findings", FINDINGS_FIXTURE_SCHEMAS],
  ]) {
    const onDisk = [
      ...(await listFixtureRels(`${dirName}/valid`)),
      ...(await listFixtureRels(`${dirName}/invalid`)),
    ];
    const missing = onDisk.filter((r) => !Object.hasOwn(table, r));
    const extra = Object.keys(table).filter((r) => !onDisk.includes(r));
    if (missing.length) failures.push(`${dirName} 在盘 fixture 无映射（死文件）：${missing.join(", ")}`);
    if (extra.length) failures.push(`${dirName} 映射指向不存在的 fixture：${extra.join(", ")}`);
  }
  // 豁免清单不许指向不存在的 fixture
  {
    const allInvalid = [
      ...(await listFixtureRels("events/invalid")),
      ...(await listFixtureRels("rest/invalid")),
      ...(await listFixtureRels("findings/invalid")),
    ];
    const ghosts = Object.keys(INVALID_EXEMPTIONS).filter((r) => !allInvalid.includes(r));
    if (ghosts.length) failures.push(`INVALID_EXEMPTIONS 指向不存在的 fixture：${ghosts.join(", ")}`);
  }

  // 3) events 全部 fixture → EventUnionSchema
  for (const rel of await listFixtureRels("events/valid")) {
    await preload(rel);
    checkFixture(rel, union, true);
  }
  for (const rel of await listFixtureRels("events/invalid")) {
    await preload(rel);
    checkFixture(rel, union, false);
  }

  // 4) rest / findings → 显式映射的 DTO schema
  for (const [dirName, table] of [
    ["rest", REST_FIXTURE_SCHEMAS],
    ["findings", FINDINGS_FIXTURE_SCHEMAS],
  ]) {
    for (const rel of await listFixtureRels(`${dirName}/valid`)) {
      if (!Object.hasOwn(table, rel)) continue;
      await preload(rel);
      checkFixture(rel, await loadSchema(table[rel]), true);
    }
    for (const rel of await listFixtureRels(`${dirName}/invalid`)) {
      if (!Object.hasOwn(table, rel)) continue;
      await preload(rel);
      checkFixture(rel, await loadSchema(table[rel]), false);
    }
  }

  // 5) 汇总
  const exemptCount = Object.keys(INVALID_EXEMPTIONS).length;
  console.log(
    `fixture↔zod 校验：valid 接受 ${stats.validAccepted}/${stats.validTotal}；` +
      `invalid 拒绝 ${stats.invalidRejected}/${stats.invalidTotal}（豁免 ${stats.exempted}/${exemptCount}：仅跨字段 model_validator）`,
  );
  if (exemptCount > 0) {
    console.log(`  豁免清单（逐条原因见 validate_fixtures.mjs INVALID_EXEMPTIONS）：`);
    for (const [rel, reason] of Object.entries(INVALID_EXEMPTIONS)) {
      console.log(`    - ${rel} — ${reason}`);
    }
  }
  if (failures.length) {
    console.error(`\nFAIL: fixture↔zod 校验未通过（${failures.length} 项）：`);
    for (const f of failures) console.error(`  - ${f}`);
    process.exit(1);
  }
  console.log("fixtures-validate: OK");
}

await main();
