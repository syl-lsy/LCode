"""配置中心：`BaseSettingsWithEnv` 基类 + 按 env 前缀分组（A3 详设 §2）。

约定（基座铁律 1/5 + A3 §2.1）：

- 本文件是配置的**唯一定义处**：业务代码一律 `from app.core.config import xxx_settings`
  读取，禁止在业务代码里硬编码连接串/密钥；
- `env_file=".env"` 相对**运行时 cwd** 解析（基座铁律 6）：所有命令统一从仓库根目录跑；
- 密钥只以「环境变量名引用」形式出现（`api_key_env`，A3 §2.2 / NFR-004）——配置文件、
  settings 快照与日志里**永不出现密钥本体**，运行时由工厂 `os.environ[...]` 取值；
- 新增配置变量必须同步仓库根 `.env.example` 与 `.env.test.example`；
- 分组前缀清单（与 `.env.example` 逐项对齐）：通用（无前缀）/ `WEB_` / `DB_` / `LLM_` /
  `SANDBOX_` / `SSE_` / `AGENT_`。
"""

from pydantic_settings import BaseSettings


class BaseSettingsWithEnv(BaseSettings):
    """所有配置组的基类：统一 `.env` 装载与未知变量容忍。

    - `env_file=".env"`：相对运行时 cwd 解析（仓库根），不入库（见根 .gitignore）；
    - `extra="ignore"`：`.env` 中未被任何分组消费的变量（如 CI 注入的 `CI`/`GITHUB_*`）不报错。
    """

    model_config = {"env_file": ".env", "extra": "ignore"}


# ---------------------------------------------------------------- 通用（无前缀）


class CommonSettings(BaseSettingsWithEnv):
    """通用配置：环境标识与日志级别。"""

    environment: str = "development"
    """部署环境；`production` 时隐藏 /docs、/redoc、/openapi.json（基座口径）。"""

    log_level: str = "INFO"
    """应用日志级别（NFR-007）；main.py 启动时应用到 app logger 与 root logger。"""


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


class LLMSettings(BaseSettingsWithEnv):
    """模型网关配置（`LLM_` 前缀）：OpenAI 兼容网关口径（AGENTS.md 技术栈冻结）。

    骨架阶段 `base_url` 允许为空（不阻塞无 LLM 的冒烟验证）；M2 的
    `get_llm(role)` 工厂在首次取用时 fail-fast 校验缺失（A3 §3.1）。
    """

    base_url: str = ""  # LLM_BASE_URL：网关地址（百炼兼容模式端点）
    api_key_env: str = "DASHSCOPE_API_KEY"  # LLM_API_KEY_ENV：密钥的**环境变量名**，非密钥本体
    request_timeout_s: int = 300  # LLM_REQUEST_TIMEOUT_S（NFR-005）
    max_retries: int = 2  # LLM_MAX_RETRIES
    model_config = {"env_prefix": "LLM_"}


# ---------------------------------------------------------------- SANDBOX_


class SandboxSettings(BaseSettingsWithEnv):
    """沙箱配置（`SANDBOX_` 前缀）：provider 开关 + 双适配器参数 + 计费口径（A3 §4）。

    provider 切换粒度 = 会话级（AC-002-6）：新会话创建时把当前值固化到
    `sessions.sandbox_provider` 列，改全局配置只影响新会话。
    """

    provider: str = "bailian"  # SANDBOX_PROVIDER: bailian | opensandbox（AC-002-6）
    idle_pause_seconds: int = 600  # SANDBOX_IDLE_PAUSE_SECONDS：空闲多久显式 pause（AC-002-2）
    cost_rate_per_hour: float = 0.156  # SANDBOX_COST_RATE_PER_HOUR：running 单价（AC-015-4）
    paused_rate_per_gib_hour: float = 0.0021  # SANDBOX_PAUSED_RATE_PER_GIB_HOUR（AC-015-4）

    # 百炼（E2B 协议；e2b==2.31.0 钉死，理由见 pyproject.toml 注释）
    bailian_api_key_env: str = "BAILIAN_API_KEY"  # SANDBOX_BAILIAN_API_KEY_ENV：密钥的环境变量名
    bailian_template: str = ""  # SANDBOX_BAILIAN_TEMPLATE：镜像模板（M2 spike 定，U5）
    bailian_domain: str = ""  # SANDBOX_BAILIAN_DOMAIN：E2B 协议域名（M2 spike 定）

    # OpenSandbox（自托管备选；自有协议，非 E2B 兼容——AGENTS.md 硬约束 12）
    opensandbox_endpoint: str = ""  # SANDBOX_OPENSANDBOX_ENDPOINT：按实际部署端口填写
    opensandbox_api_key_env: str = "OPENSANDBOX_API_KEY"  # SANDBOX_OPENSANDBOX_API_KEY_ENV
    opensandbox_backend: str = "docker"  # SANDBOX_OPENSANDBOX_BACKEND: docker | k8s | firecracker
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
    model_config = {"env_prefix": "AGENT_"}


# ---------------------------------------------------------------- 单例（全项目唯一读取入口）

common_settings = CommonSettings()
web_settings = WebSettings()
db_settings = DBSettings()
llm_settings = LLMSettings()
sandbox_settings = SandboxSettings()
sse_settings = SSESettings()
agent_settings = AgentSettings()
