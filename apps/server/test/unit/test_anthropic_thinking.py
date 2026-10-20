"""unit：Anthropic thinking/signature round-trip 回归锚（ADR-007 核心证据）。

背景（ADR-007）：langchain-openai 1.7.2 的 ChatOpenAI 源码 docstring 明确第三方
`reasoning_content` / `reasoning_details` 不提取不保留，`_convert_dict_to_message` /
`_convert_delta_to_message_chunk` 实测丢弃——源思维链在 OpenAI 兼容路径不可保留。
langchain-anthropic 1.7.6 则把响应 `thinking` 块（含 `signature`）放进 AIMessage.content，
且 `_format_messages` 把 `{"type": "thinking", ..., "signature": ...}` 与
`redacted_thinking` 原样发回——多轮对话中历史思考块可无损回传。

本文件用安装版 `langchain_anthropic.chat_models._format_messages` 做静态 round-trip：
带 thinking+signature 的 AIMessage → 请求 dict，断言块与签名逐字节保留。这是
"切 ChatAnthropic 才能保留源思维链"这一决策的核心回归锚；升级 langchain-anthropic
若破坏该行为，这里先红。

零网络：仅调用本地纯函数，不触任何网关。
"""

from langchain_anthropic.chat_models import _format_messages
from langchain_core.messages import AIMessage


def test_format_messages_preserves_thinking_block_with_signature():
    """thinking 块（含 signature）原样回传：源思维链保留的核心证据。"""

    message = AIMessage(
        content=[
            {"type": "thinking", "thinking": "先想再答", "signature": "sig-abc-123"},
            {"type": "text", "text": "答案"},
        ]
    )
    _, formatted = _format_messages([message], model="qwen3.7-plus")
    assert len(formatted) == 1
    assert formatted[0]["role"] == "assistant"
    blocks = formatted[0]["content"]
    assert isinstance(blocks, list)
    thinking_block = blocks[0]
    assert thinking_block["type"] == "thinking"
    assert thinking_block["thinking"] == "先想再答"
    assert thinking_block["signature"] == "sig-abc-123"
    # text 块不受影响
    assert blocks[1] == {"type": "text", "text": "答案"}


def test_format_messages_preserves_redacted_thinking():
    """redacted_thinking 块同样原样回传（Anthropic 侧加密思考的往返形态）。"""

    message = AIMessage(
        content=[
            {"type": "redacted_thinking", "data": "redacted-opaque-blob"},
            {"type": "text", "text": "答案"},
        ]
    )
    _, formatted = _format_messages([message], model="qwen3.7-plus")
    blocks = formatted[0]["content"]
    assert isinstance(blocks, list)
    assert blocks[0]["type"] == "redacted_thinking"
    assert blocks[0]["data"] == "redacted-opaque-blob"


def test_format_messages_signature_only_thinking_defaults_empty_text():
    """signature 在而 thinking 空串的历史块：回传时补 thinking 空串占位（源码 872-873 行口径）。"""

    message = AIMessage(content=[{"type": "thinking", "signature": "sig-only"}])
    _, formatted = _format_messages([message], model="qwen3.7-plus")
    block = formatted[0]["content"][0]
    assert block["type"] == "thinking"
    assert block["signature"] == "sig-only"
    assert block["thinking"] == ""
