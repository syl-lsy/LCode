# contracts/generated/ — 契约生成产物（必须入库）

本目录内容全部由工具生成，**禁止手改**；修改契约只能改
`contracts/src/lcode_contracts/`（单源），然后重新生成：

```bash
make -C contracts gen    # 生成 generated/json（+ node 可用时生成 generated/ts）
```

- `json/`：pydantic 模型导出的 JSON Schema（TS 生成与文档化共用）；
- `ts/`：`json/` 经 json-schema-to-typescript（类型）与 json-schema-to-zod
  （运行时校验）生成，供 `web/` 以相对路径或包方式引入（阶段 2 接入）。

`make -C contracts check` 会校验生成物与单源同步（过期/缺失即失败）。
