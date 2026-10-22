"""配置中心：`BaseSettingsWithEnv` 基类 + 按 env 前缀分组（A3 详设 §2）。

约定（基座铁律 1/5 + A3 §2.1）：

- 本文件是配置的**唯一定义处**：业务代码一律 `from app.core.config import xxx_settings`
  读取，禁止在业务代码里硬编码连接串/密钥；
- `env_file=".env"` 相对**运行时 cwd** 解析（基座铁律 6）：所有命令统一从仓库根目录跑；
- 密钥只以「环境变量名引用」形式出现（`api_key_env`，A3 §2.2 / NFR-004）——配置文件、
  settings 快照与日志里**永不出现密钥本体**，运行时由工厂 `os.environ[...]` 取值；
- 新增配置变量必须同步仓库根 `.env.example` 与 `.env.test.example`；
- 分组前缀清单（与 `.env.example` 逐项对齐）：通用（无前缀）/ `WEB_` / `DB_` / `LLM_` /
  `SANDBOX_` / `SSE_` / `AGENT_` / `LOG_`。
"""

import os
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings


class BaseSettingsWithEnv(BaseSettings):
    """所有配置组的基类：统一 `.env` 装载与未知变量容忍。

    - `env_file=".env"`：相对运行时 cwd 解析（仓库根），不入库（见根 .gitignore）；
    - `extra="ignore"`：`.env` 中未被任何分组消费的变量（如 CI 注入的 `CI`/`GITHUB_*`）不报错。
    """

    model_config = {"env_file": ".env", "extra": "ignore"}


# ---------------------------------------------------------------- 环境变量名安全渲染

_UNSAFE_ENV_VAR_NAME = "<非法的环境变量名：疑似填入了密钥本体>"
"""非法变量名（或误填密钥本体）的固定占位文本——绝不回显原值（NFR-004）。"""

_ENV_VAR_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
"""POSIX 风格环境变量名形态（字母/下划线开头，仅字母数字下划线）。"""


def safe_env_var_name(name: str) -> str:
    """把 `*_API_KEY_ENV` 字段安全地渲染进消息/报告。

    该字段的语义是「环境变量名」，但误填密钥本体时直接插值会把密钥带进
    生产错误响应与日志（NFR-004）。合法变量名原样返回；其余一律替换为固定
    占位文本，**不回显原值**。
    """
    return name if _ENV_VAR_NAME_PATTERN.fullmatch(name) else _UNSAFE_ENV_VAR_NAME


# ---------------------------------------------------------------- 通用（无前缀）


class CommonSettings(BaseSettingsWithEnv):
    """通用配置：环境标识（日志级别已迁往 `LogSettings.level`，env 键名 `LOG_LEVEL` 不变）。"""

    environment: str = "development"
    """部署环境；`production` 时隐藏 /docs、/redoc、/openapi.json（基座口径）。"""


# ---------------------------------------------------------------- WEB_


class WebSettings(BaseSettingsWithEnv):
    """Web 服务配置（`WEB_` 前缀）：开发期监听地址与展示名。"""

    app_name: str = "LCode Server API"  # WEB_APP_NAME
    host: str = "127.0.0.1"  # WEB_HOST
    port: int = 8000  # WEB_PORT
    model_config = {"env_prefix": "WEB_"}


# ---------------------------------------------------------------- DB_


class DBSettings(BaseSettingsWithEnv):
    """数据库配置（`DB_` 前缀）：业务十表 + LangGraph checkpointer 共用同一个 PG 实例。

    连接池参数集中在此，由 `core/db.py` 的引擎构造消费（基座铁律 2）。
    """

    host: str = "localhost"
    port: int = 5432
    name: str = "lcode"
    user: str = "lcode"
    password: str = ""  # 密钥本体只进 .env（不入库）；空表示本地信任连接
    pool_size: int = 5
    max_overflow: int = 10
    echo: bool = False  # 打开后 SQLAlchemy 打印 SQL（排查用）
    model_config = {"env_prefix": "DB_"}


# ---------------------------------------------------------------- LLM_


def _current_environment() -> str:
    """当前部署环境标识：进程环境变量优先；未 export 时按 CommonSettings 同口径读 .env。"""
    return os.environ.get("ENVIRONMENT") or CommonSettings().environment


class LLMSettings(BaseSettingsWithEnv):
    """模型网关配置（`LLM_` 前缀）：Anthropic Messages 兼容网关口径（ADR-007）。

    `base_url` 必须是 Anthropic Messages 兼容端点（`/v1/messages`）；OpenAI-only
    网关不支持。骨架阶段 `base_url` 允许为空（不阻塞无 LLM 的冒烟验证）；M2 的
    `get_llm(role)` 工厂在首次取用时 fail-fast 校验缺失（A3 §3.1）。

    C7 scheme 校验（M2 收口）：非空 `base_url` 只接受 http/https；`ENVIRONMENT=production`
    时强制 https（http 拒绝）；ftp/file/data/无 scheme 一律拒绝。空串 = 未配置，放行。
    错误信息只点名字段与允许的 scheme——**不回显 URL**（pydantic 默认会在
    ValidationError 里附 `input_value`，故开启 `hide_input_in_errors`，避免 userinfo
    中的密码随异常/日志泄漏）。
    """

    # ENVIRONMENT 属通用组：这里不新增字段（避免 LLM_ENVIRONMENT 假变量进模板），
    # 校验时按 CommonSettings 同口径读取（进程环境变量优先，否则读 .env）。
    base_url: str = ""  # LLM_BASE_URL：Anthropic Messages 兼容网关地址（/v1/messages 端点）
    api_key_env: str = "DASHSCOPE_API_KEY"  # LLM_API_KEY_ENV：密钥的**环境变量名**，非密钥本体
    request_timeout_s: int = 300  # LLM_REQUEST_TIMEOUT_S（NFR-005）
    max_retries: int = 2  # LLM_MAX_RETRIES
    model_config = {"env_prefix": "LLM_", "hide_input_in_errors": True}

    @model_validator(mode="after")
    def _validate_base_url_scheme(self) -> LLMSettings:
        """C7：空串放行；非空仅 http/https；production 仅 https。

        报错文案刻意不引用 `base_url` 本体（含 userinfo 时可能带密码），只写字段名
        与允许的 scheme；`hide_input_in_errors` 保证 ValidationError 也不回显输入值。
        """
        base_url = self.base_url.strip()
        if not base_url:
            self.base_url = ""
            return self

        scheme = urlsplit(base_url).scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError(
                "LLM_BASE_URL 的 scheme 非法：仅允许 http/https（非空值必须显式带 scheme）"
            )
        if _current_environment().strip().lower() == "production" and scheme != "https":
            raise ValueError(
                "LLM_BASE_URL 在 ENVIRONMENT=production 时必须使用 https（仅允许 http/https，"
                "生产仅 https）"
            )
        self.base_url = base_url
        return self


# ---------------------------------------------------------------- SANDBOX_


class SandboxSettings(BaseSettingsWithEnv):
    """沙箱配置（`SANDBOX_` 前缀）：provider 开关 + 双适配器参数 + 计费口径（A3 §4）。

    provider 切换粒度 = 会话级（AC-002-6）：新会话创建时把当前值固化到
    `sessions.sandbox_provider` 列，改全局配置只影响新会话。
    """

    provider: str = "bailian"  # SANDBOX_PROVIDER: bailian | opensandbox（AC-002-6）
    workspace_dir: str = ""  # SANDBOX_WORKSPACE_DIR：diff 工作区覆盖（空 = 沙箱默认 cwd，D7）
    idle_pause_seconds: int = 600  # SANDBOX_IDLE_PAUSE_SECONDS：空闲多久显式 pause（AC-002-2）
    cost_rate_per_hour: float = 0.156  # SANDBOX_COST_RATE_PER_HOUR：running 单价（AC-015-4）
    paused_rate_per_gib_hour: float = 0.0021  # SANDBOX_PAUSED_RATE_PER_GIB_HOUR（AC-015-4）

    # 百炼（E2B 协议；e2b==2.31.0 钉死，理由见 pyproject.toml 注释）
    bailian_api_key_env: str = "BAILIAN_API_KEY"  # SANDBOX_BAILIAN_API_KEY_ENV：密钥的环境变量名
    bailian_template: str = ""  # SANDBOX_BAILIAN_TEMPLATE：镜像模板（M2 spike 定，U5）
    # SANDBOX_BAILIAN_API_URL：百炼 E2B 接入**完整地址**，形态
    # `https://{workspace_id}.{region}.maas.aliyuncs.com/api/v1/agentstudio/sandbox`
    # （region 当前仅 cn-beijing）；不要传 e2b 的 `domain`（会被拼成 https://api.<domain>）
    bailian_api_url: str = ""
    # SANDBOX_BAILIAN_ALIYUN_UID：仅用于拼 `e2b_<uid>` 形态的 api_key（满足 e2b 格式校验），
    # 百炼不拿它做业务鉴权——真实鉴权是 Authorization: Bearer <BAILIAN_API_KEY>
    bailian_aliyun_uid: str = ""

    # OpenSandbox（自托管备选；自有协议，非 E2B 兼容——AGENTS.md 硬约束 12）
    opensandbox_endpoint: str = ""  # SANDBOX_OPENSANDBOX_ENDPOINT：按实际部署端口填写
    opensandbox_api_key_env: str = "OPENSANDBOX_API_KEY"  # SANDBOX_OPENSANDBOX_API_KEY_ENV
    opensandbox_backend: str = "docker"  # SANDBOX_OPENSANDBOX_BACKEND: docker | k8s | firecracker
    # SANDBOX_OPENSANDBOX_IMAGE：SDK 要求 image 或 snapshot_id（本适配器不支持 snapshot）；
    # 留空 = 未配置，create 时 fail-fast；不内置默认镜像
    opensandbox_image: str = ""
    model_config = {"env_prefix": "SANDBOX_"}


# ---------------------------------------------------------------- SSE_


class SSESettings(BaseSettingsWithEnv):
    """SSE 传输参数（`SSE_` 前缀）：契约 `rest.py` 模块 docstring 的传输语义落点。"""

    retry_ms: int = 3000  # SSE_RETRY_MS：连接建立即发的首帧 retry 值
    heartbeat_seconds: int = 15  # SSE_HEARTBEAT_SECONDS：注释行心跳间隔
    subscriber_queue: int = 1000  # SSE_SUBSCRIBER_QUEUE：per-subscriber 有界队列（满则断连重放）
    model_config = {"env_prefix": "SSE_"}


# ---------------------------------------------------------------- AGENT_


class AgentSettings(BaseSettingsWithEnv):
    """编排运行时参数（`AGENT_` 前缀）。"""

    max_parallel_fanout: int = 8  # AGENT_MAX_PARALLEL_FANOUT：Send×N 并行上限（REQ-009 / NFR-005）
    run_timeout_s: int = 3600  # AGENT_RUN_TIMEOUT_S：单 run 墙钟超时
    checkpointer: Literal["postgres", "memory"] = "postgres"  # AGENT_CHECKPOINTER
    """断点恢复 saver 后端（REQ-004 约束④，m3-spikes §2）。

    - `postgres`（默认）：lifespan 期建 `AsyncPostgresSaver`（跨进程/跨事件循环 resume，
      审批 interrupt 恢复的部署口径）；
    - `memory`：不建真库 saver（进程内 `InMemorySaver` 生效）——测试车道用，零副作用。
    `LCODE_E2E_CHECKPOINTER` 仍是 e2e 的强制口（覆盖本配置，见 core/postgres_checkpointer）。
    """

    auto_approval_classifier: Literal["rules", "llm"] = "rules"  # AGENT_AUTO_APPROVAL_CLASSIFIER
    """AUTO 审批分类器实现（REQ-013 AC-013-3）：rules=确定性规则（默认，零网络）/
    llm=经济档 `approval_classifier` 角色。默认 rules 保证测试与无凭证环境零网络。
    """

    auto_approval_timeout_s: float = 8.0  # AGENT_AUTO_APPROVAL_TIMEOUT_S
    """LLM 分类器单次决策的墙钟上限（秒）；超时即 fail-closed（转人工审批）。"""

    model_config = {"env_prefix": "AGENT_"}


# ---------------------------------------------------------------- LOG_


class LogSettings(BaseSettingsWithEnv):
    """日志配置（`LOG_` 前缀，统一日志体系见 `app/core/logger/`；基座铁律 11 / NFR-007）。

    `log_level` 自 `CommonSettings` 迁入（env 键名 `LOG_LEVEL` 不变）：日志级别只归
    loguru 管线消费，与通用组解耦。
    """

    level: str = "INFO"  # LOG_LEVEL：输出级别（TRACE/DEBUG/INFO/SUCCESS/WARNING/ERROR/CRITICAL）
    slow_query_threshold: float = 200  # LOG_SLOW_QUERY_THRESHOLD：SQL 超过该毫秒数记 DBLog.warning
    file_dir: str = "logs"  # LOG_FILE_DIR：dev 环境 ERROR 日志目录（相对运行时 cwd 即仓库根）
    model_config = {"env_prefix": "LOG_"}


# ---------------------------------------------------------------- 单例（全项目唯一读取入口）

common_settings = CommonSettings()
web_settings = WebSettings()
db_settings = DBSettings()
llm_settings = LLMSettings()
sandbox_settings = SandboxSettings()
sse_settings = SSESettings()
agent_settings = AgentSettings()
log_settings = LogSettings()
