"""unit 层：settings 分组的实例化、前缀隔离与默认值断言（零网络）。

覆盖 A3 §2.1 配置清单的关键口径：分组前缀互不串扰、密钥字段存的是**环境变量名**
（引用模式，NFR-004）而非密钥本体、计费与传输参数默认值稳定。

用例一律走 `_fresh()`（跳过 .env）：断言的是**默认值/进程环境变量**，与本地 .env
内容无关——否则开发机上的 .env 会让用例结果漂移。
"""

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings

from app.core.config import (
    AgentSettings,
    CommonSettings,
    DBSettings,
    LLMSettings,
    LogSettings,
    SandboxSettings,
    SSESettings,
    WebSettings,
    safe_env_var_name,
)


def _fresh[T: BaseSettings](settings_cls: type[T]) -> T:
    """实例化某个配置组并跳过 .env（只读进程环境变量）。

    `_env_file=None` 是 pydantic-settings 的运行期关键字（跳过 `.env` 装载），
    保证用例断言的是默认值/进程环境变量，与开发机上的 .env 内容无关。
    """
    return settings_cls(_env_file=None)


def test_common_settings_default(monkeypatch):
    """通用组（无前缀）：默认值直接生效（log_level 已迁往 LogSettings，见下方 LOG_ 组）。"""
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    settings = _fresh(CommonSettings)
    assert settings.environment == "development"
    assert "log_level" not in CommonSettings.model_fields  # 防回流：日志级别只归 LOG_ 组


def test_log_settings_defaults(monkeypatch):
    """LOG_ 组默认口径：级别 INFO、慢查询阈值 200ms、dev 错误日志目录 logs（基座铁律 11）。"""
    for var in ("LOG_LEVEL", "LOG_SLOW_QUERY_THRESHOLD", "LOG_FILE_DIR"):
        monkeypatch.delenv(var, raising=False)
    settings = _fresh(LogSettings)
    assert settings.level == "INFO"
    assert settings.slow_query_threshold == 200
    assert settings.file_dir == "logs"


def test_log_settings_reads_prefixed_env(monkeypatch):
    """LOG_ 前缀：LOG_LEVEL / LOG_SLOW_QUERY_THRESHOLD / LOG_FILE_DIR 映射到字段（阈值转 float）。"""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LOG_SLOW_QUERY_THRESHOLD", "50")
    monkeypatch.setenv("LOG_FILE_DIR", "tmp/logs")
    settings = _fresh(LogSettings)
    assert settings.level == "DEBUG"
    assert settings.slow_query_threshold == 50
    assert settings.file_dir == "tmp/logs"


def test_log_prefix_is_isolated_from_other_groups(monkeypatch):
    """前缀隔离：LOG_ 组的变量不会落入 DB_ 组（同名后缀互不串扰），反之亦然。"""
    monkeypatch.setenv("LOG_NAME", "should-not-leak")
    monkeypatch.delenv("DB_NAME", raising=False)
    assert _fresh(DBSettings).name == "lcode"  # 默认值，未被 LOG_NAME 污染
    monkeypatch.setenv("DB_LEVEL", "ERROR")
    assert _fresh(LogSettings).level == "INFO"  # 默认值，未被 DB_LEVEL 污染


def test_web_settings_reads_prefixed_env(monkeypatch):
    """WEB_ 前缀：WEB_APP_NAME / WEB_PORT 映射到 app_name / port（且 port 转 int）。"""
    monkeypatch.setenv("WEB_APP_NAME", "订单服务 API")
    monkeypatch.setenv("WEB_PORT", "9000")
    settings = _fresh(WebSettings)
    assert settings.app_name == "订单服务 API"
    assert settings.port == 9000


def test_db_settings_reads_prefixed_env(monkeypatch):
    """DB_ 前缀：连接参数与连接池参数（A3 §2.1 表）都能读到。"""
    monkeypatch.setenv("DB_HOST", "db.internal")
    monkeypatch.setenv("DB_POOL_SIZE", "7")
    monkeypatch.setenv("DB_ECHO", "true")
    settings = _fresh(DBSettings)
    assert settings.host == "db.internal"
    assert settings.pool_size == 7
    assert settings.echo is True


def test_prefix_isolation(monkeypatch):
    """前缀隔离：WEB_ 组的变量不会落入 DB_ 组（同名后缀互不串扰）。"""
    monkeypatch.setenv("WEB_NAME", "should-not-leak")
    monkeypatch.delenv("DB_NAME", raising=False)
    assert _fresh(DBSettings).name == "lcode"  # 默认值，未被 WEB_NAME 污染


def test_llm_settings_store_key_env_name_not_secret(monkeypatch):
    """密钥引用模式（NFR-004 / A3 §2.2）：LLM_ 组只存环境变量名，不存密钥本体。"""
    monkeypatch.delenv("LLM_API_KEY_ENV", raising=False)
    # 进程隔离：integration conftest 收集期 load_dotenv(.env.test) 会注入真实 LLM_BASE_URL
    # （`_env_file=None` 只跳过 .env，仍读进程环境），不清掉会让本用例随环境漂移。
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    settings = _fresh(LLMSettings)
    assert settings.api_key_env == "DASHSCOPE_API_KEY"
    assert settings.base_url == ""  # 骨架期允许为空；M2 工厂取用时 fail-fast
    assert "api_key" not in settings.model_dump()  # 字段面本身没有密钥本体字段


def test_sandbox_settings_defaults(monkeypatch):
    """沙箱默认口径：provider=bailian、空闲 pause 10 分钟、双计费单价（AC-002-2/015-4）。"""
    for var in (
        "SANDBOX_PROVIDER",
        "SANDBOX_IDLE_PAUSE_SECONDS",
        "SANDBOX_COST_RATE_PER_HOUR",
        "SANDBOX_PAUSED_RATE_PER_GIB_HOUR",
    ):
        monkeypatch.delenv(var, raising=False)
    settings = _fresh(SandboxSettings)
    assert settings.provider == "bailian"
    assert settings.idle_pause_seconds == 600
    assert settings.cost_rate_per_hour == 0.156
    assert settings.paused_rate_per_gib_hour == 0.0021
    # 两个 provider 的密钥都只存引用名
    assert settings.bailian_api_key_env == "BAILIAN_API_KEY"
    assert settings.opensandbox_api_key_env == "OPENSANDBOX_API_KEY"
    assert settings.opensandbox_backend == "docker"  # macOS 本地退化后端


def test_sse_and_agent_settings_defaults(monkeypatch):
    """SSE 传输语义默认值（契约 rest.py：retry 3000 / 心跳 15s）与并行上限。"""
    for var in ("SSE_RETRY_MS", "SSE_HEARTBEAT_SECONDS", "AGENT_MAX_PARALLEL_FANOUT"):
        monkeypatch.delenv(var, raising=False)
    assert _fresh(SSESettings).retry_ms == 3000
    assert _fresh(SSESettings).heartbeat_seconds == 15
    assert _fresh(AgentSettings).max_parallel_fanout == 8


# ---------------------------------------------------------------- C7: LLM_BASE_URL scheme 校验


def _llm_settings_with(
    monkeypatch: pytest.MonkeyPatch, *, base_url: str, environment: str
) -> LLMSettings:
    """按显式基础地址/环境实例化 LLM_ 组（跳过 .env，只看进程环境变量）。"""
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("LLM_BASE_URL", base_url)
    return _fresh(LLMSettings)


def test_llm_base_url_accepts_https(monkeypatch):
    """https 在任何环境都合法（C7：非空时只接受 http/https）。"""
    settings = _llm_settings_with(
        monkeypatch, base_url="https://gateway.example/v1", environment="development"
    )
    assert settings.base_url == "https://gateway.example/v1"


def test_llm_base_url_accepts_https_in_production(monkeypatch):
    """production + https：唯一被允许的生产组合。"""
    settings = _llm_settings_with(
        monkeypatch, base_url="https://gateway.example/v1", environment="production"
    )
    assert settings.base_url == "https://gateway.example/v1"


def test_llm_base_url_accepts_http_in_development(monkeypatch):
    """development + http 放行（本地/内网网关调试），便于 M2 spike。"""
    settings = _llm_settings_with(
        monkeypatch, base_url="http://127.0.0.1:8080/v1", environment="development"
    )
    assert settings.base_url == "http://127.0.0.1:8080/v1"


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://gateway.example/v1",
        "file:///tmp/gateway",
        "data:text/plain,x",
        "gateway.example/v1",  # 无 scheme
        "//gateway.example/v1",  # 协议相对，无 scheme
    ],
)
def test_llm_base_url_rejects_non_http_schemes(monkeypatch, base_url):
    """ftp/file/data/无 scheme 一律拒绝（C7）。"""
    with pytest.raises(ValidationError, match="LLM_BASE_URL"):
        _llm_settings_with(monkeypatch, base_url=base_url, environment="development")


def test_llm_base_url_rejects_http_in_production(monkeypatch):
    """production + http 拒绝：生产强制 https（C7）。"""
    with pytest.raises(ValidationError, match="https"):
        _llm_settings_with(
            monkeypatch, base_url="http://gateway.example/v1", environment="production"
        )


def test_llm_base_url_allows_empty(monkeypatch):
    """空串允许（未配置）：production 下同样放行，缺失由 get_llm 调用期 fail-fast。"""
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert _fresh(LLMSettings).base_url == ""


def test_llm_base_url_error_never_echoes_userinfo_credentials(monkeypatch):
    """非法 URL 的报错不回显 userinfo/密码/完整 URL（只写字段名与允许 scheme）。"""
    password = "s3cr3t-pa55"
    base_url = f"http://alice:{password}@gateway.example/v1"
    with pytest.raises(ValidationError) as exc_info:
        _llm_settings_with(monkeypatch, base_url=base_url, environment="production")
    message = str(exc_info.value)
    assert "LLM_BASE_URL" in message
    assert password not in message
    assert "alice" not in message
    assert base_url not in message


def test_unknown_env_vars_are_ignored(tmp_path, monkeypatch):
    """extra="ignore"：.env 里非配置变量（CI 注入的 CI/GITHUB_* 之类）不报错。"""
    env_file = tmp_path / ".env"
    env_file.write_text("CI=true\nWEB_PORT=8123\n", encoding="utf-8")
    monkeypatch.delenv("WEB_PORT", raising=False)

    settings = WebSettings(_env_file=str(env_file))  # type: ignore[call-arg]

    assert settings.port == 8123


# ---------------------------------------------------------------- 环境变量名安全渲染


def test_safe_env_var_name_passes_legal_name_through():
    """合法环境变量名（字母/下划线开头，仅字母数字下划线）原样返回。"""
    assert safe_env_var_name("BAILIAN_API_KEY") == "BAILIAN_API_KEY"
    assert safe_env_var_name("_x9") == "_x9"


@pytest.mark.parametrize(
    "misconfigured",
    [
        "sk-unit-test-secret",  # 误填密钥本体
        "sk-ant-api03-abc.def",  # 密钥形态 + 非法字符
        "9-START-DIGIT",  # 数字开头
        "with space",
        "",
    ],
)
def test_safe_env_var_name_redacts_anything_else(misconfigured: str):
    """字段语义是「变量名」：非法形态一律固定占位，绝不回显原值（NFR-004）。"""
    redacted = safe_env_var_name(misconfigured)

    assert redacted == "<非法的环境变量名：疑似填入了密钥本体>"
    if misconfigured:
        assert misconfigured not in redacted
