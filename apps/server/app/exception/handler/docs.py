"""API 文档错误码表：从 `errors.py` 的两张表自动生成 Markdown，注入 `FastAPI(description=...)`。

新增错误码注册后自动出现在 /docs 的「错误码说明」里，不需要手写文档。
生成物同时是契约错误表的镜像：业务码列来自契约 `RestErrorCode`（ADR-004 偏差 9），
`docs.py` 不引入任何本地码值。
"""

from app.exception.handler.errors import ERROR_MAP, PROTOCOL_ERROR_MAP

_TABLE_HEADER = "| 业务错误码 | HTTP状态码 | 消息 |"


def generate_error_docs() -> str:
    """生成「## 错误码说明」：业务错误表（兜底行不列、同码去重）+ 协议错误表。"""
    lines: list[str] = [
        "## 错误码说明",
        "",
        "### 业务错误",
        "",
        _TABLE_HEADER,
        "|---|---|---|",
    ]
    seen_codes: set[str] = set()
    for exc_type, (code, http_status, message) in ERROR_MAP.items():
        if exc_type is Exception:  # 兜底码不面向客户端文档（客户端无需区分内部错误成因）
            continue
        if code in seen_codes:  # 多个异常映射同一契约码（如校验失败与语义级非法入参同为
            # INVALID_ARGUMENT）只列一次
            continue
        seen_codes.add(code)
        lines.append(f"| {code} | {http_status} | {message} |")

    lines += ["", "### 协议错误", "", _TABLE_HEADER, "|---|---|---|"]
    for status, message in PROTOCOL_ERROR_MAP.items():
        lines.append(f"| {status} | {status} | {message} |")

    return "\n".join(lines)
