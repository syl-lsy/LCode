"""GET /models：模型列表端点（AC-014-1；A3 §10 任务 3）。

注册表直出（A3 §3.2）：`registry.list_models()` 的 `reasoning_efforts` 已按
`supports_reasoning` 过滤（非 reasoning 模型恒空表），是前端模型选择器 +
effort 档位置灰的唯一直接数据源。路由层零业务逻辑（注册表在 import 期已
完成 fail-fast 校验，端点不做二次校验）。

依赖注入经 `app.dependencies.ModelRegistryDep` 取注册表单例——
`api/` 不直接 import `agent/`（A3 §1.2 禁止表，类型引用也不留）。
"""

from fastapi import APIRouter

from app.dependencies import ModelRegistryDep
from lcode_contracts.rest import ModelList

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=ModelList, summary="模型列表（含可选 reasoning 档位）")
def list_models(registry: ModelRegistryDep) -> ModelList:
    """模型列表：`reasoning_efforts` 为过滤后的可选档位（AC-014-3 置灰依据）。"""
    return registry.list_models()
