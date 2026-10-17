#!/usr/bin/env node
/**
 * generated/json/**.json -> generated/ts/**.types.ts + **.zod.ts + 各目录 index.ts
 *
 * - TS 接口名取 schema.title（= pydantic 模型类名）；title 非合法 TS 标识符时
 *   回退文件名词的 PascalCase（如 event_union 的 RootModel[Union[...]] → EventUnion）；
 * - zod 导出名 = <接口名>Schema；对生成器 API 版本差异做了 export/import 兜底；
 * - 各目录 index.ts 只显式转发主类型/主 schema（嵌套标题生成的辅助别名跨文件同名）；
 * - zod 侧先对 $defs 深度内联再生成（json-schema-to-zod 不解析 $ref，不内联则全落 z.any()）；
 * - TS 侧校验强度为 JSON Schema 可表达子集：跨字段 model_validator 只在 Python 侧强制
 *   （实测口径 = tools/ts/validate_fixtures.mjs 的豁免清单，见 README「两侧校验强度差异」）。
 */
import { readdir, readFile, writeFile, mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { compile } from "json-schema-to-typescript";
import { jsonSchemaToZod } from "json-schema-to-zod";

const here = path.dirname(fileURLToPath(import.meta.url));
const JSON_DIR = path.resolve(here, "../../generated/json");
const OUT_DIR = path.resolve(here, "../../generated/ts");

async function listJson(dir, prefix = "") {
  const out = [];
  for (const entry of await readdir(path.join(dir, prefix), { withFileTypes: true })) {
    const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory()) {
      out.push(...(await listJson(dir, rel)));
    } else if (entry.name.endsWith(".json")) {
      out.push(rel);
    }
  }
  return out;
}

function pascal(stem) {
  return stem
    .split(/[-_]/)
    .map((s) => s.charAt(0).toUpperCase() + s.slice(1))
    .join("");
}

function isValidTsIdentifier(name) {
  return typeof name === "string" && /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(name);
}

/** schema.title 优先；非法标识符（如 pydantic 泛型别名 RootModel[Union[...]]）回退文件名词 */
function typeNameFrom(schema, parsed) {
  return isValidTsIdentifier(schema.title) ? schema.title : pascal(parsed.name);
}

/**
 * $defs 深度内联解引用（仅用于 zod 生成）。
 *
 * json-schema-to-zod v2 不解析 $ref（源码无 $ref 分支）——不内联时所有引用落 z.any()，
 * 嵌套结构（AgentRef/Usage/ToolErrorObj 等）全部失防。本契约经全量扫描确认无递归引用
 * （有向图无环、ref 全为 #/$defs/<name> 形态），深度内联安全；一旦未来引入递归引用，
 * 这里显式报错而不是死循环。types 侧（json-schema-to-typescript）原生支持 $defs，
 * 保持命名类型引用，不内联（可读性）。
 */
function inlineDefs(schema) {
  const defs = schema.$defs || {};
  const resolving = new Set();
  function resolve(node) {
    if (Array.isArray(node)) return node.map(resolve);
    if (node && typeof node === "object") {
      if (typeof node.$ref === "string") {
        const m = node.$ref.match(/^#\/\$defs\/([^/]+)$/);
        if (m && m[1] in defs) {
          const name = m[1];
          if (resolving.has(name)) {
            throw new Error(`$defs 存在递归引用（${name}），内联解引用不支持——需改造生成管线`);
          }
          resolving.add(name);
          const resolved = resolve({ ...defs[name] });
          resolving.delete(name);
          const siblings = Object.fromEntries(
            Object.entries(node).filter(([k]) => k !== "$ref"),
          );
          return Object.keys(siblings).length > 0 ? { ...resolved, ...siblings } : resolved;
        }
      }
      const out = {};
      for (const [k, v] of Object.entries(node)) {
        if (k === "$defs") continue;
        out[k] = resolve(v);
      }
      return out;
    }
    return node;
  }
  return resolve(schema);
}

/** 把生成器输出规整为"带 export 的模块"：兼容裸表达式 / 无 export 的 const / 已完整三种形态 */
function normalizeZodModule(raw, exportName) {
  let out = raw.trim();
  if (!/\bexport\b/.test(out)) {
    const constDecl = new RegExp(`(\\bconst\\s+${exportName}\\b)`);
    if (constDecl.test(out)) {
      out = out.replace(constDecl, "export $1");
    } else {
      out = `export const ${exportName} = ${out};\n`;
    }
  }
  if (!/from\s+["']zod["']/.test(out) && /\bz\./.test(out)) {
    out = `import { z } from "zod";\n${out}`;
  }
  return out.endsWith("\n") ? out : `${out}\n`;
}

const rels = await listJson(JSON_DIR);
if (rels.length === 0) {
  console.error("no json schemas under generated/json — run `make -C contracts gen` first");
  process.exit(1);
}

const entriesByDir = new Map();

for (const rel of rels) {
  const schema = JSON.parse(await readFile(path.join(JSON_DIR, rel), "utf8"));
  const parsed = path.parse(rel);
  const typeName = typeNameFrom(schema, parsed);

  // compile 的命名以 schema.title 优先于 name 参数 —— 非法 title 覆盖为派生名，
  // 保证 types 侧主类型名与 zod 侧导出名一致（其余文件 title === typeName，无影响）。
  const typesTs = await compile({ ...schema, title: typeName }, typeName, { bannerComment: "" });
  // zod 侧先做 $defs 深度内联（否则 $ref 全部落 z.any()，见 inlineDefs 注释）
  const zodTs = normalizeZodModule(
    jsonSchemaToZod(inlineDefs(schema), { name: `${typeName}Schema` }),
    `${typeName}Schema`,
  );

  const outDir = path.join(OUT_DIR, parsed.dir);
  await mkdir(outDir, { recursive: true });
  await writeFile(path.join(outDir, `${parsed.name}.types.ts`), typesTs);
  await writeFile(path.join(outDir, `${parsed.name}.zod.ts`), zodTs);

  if (!entriesByDir.has(parsed.dir)) entriesByDir.set(parsed.dir, []);
  entriesByDir.get(parsed.dir).push({ stem: parsed.name, typeName });
}

// barrel 只显式转发各文件的主类型/主 schema：嵌套 property 标题生成的辅助别名
// （Title/Status/RunState…）跨文件重名，`export *` 会触发 TS2308 歧义错误。
for (const [dir, entries] of entriesByDir) {
  const lines = entries
    .sort((a, b) => a.stem.localeCompare(b.stem))
    .flatMap(({ stem, typeName }) => [
      `export type { ${typeName} } from "./${stem}.types";`,
      `export { ${typeName}Schema } from "./${stem}.zod";`,
    ])
    .join("\n");
  await writeFile(path.join(OUT_DIR, dir, "index.ts"), `${lines}\n`);
}

const subDirs = [...entriesByDir.keys()].filter((d) => d).sort();
await writeFile(
  path.join(OUT_DIR, "index.ts"),
  subDirs.map((d) => `export * from "./${d}";`).join("\n") + "\n",
);

console.log(`generated ts for ${rels.length} schemas under contracts/generated/ts/`);
