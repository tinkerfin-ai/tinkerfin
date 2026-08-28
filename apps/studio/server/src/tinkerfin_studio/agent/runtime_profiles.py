"""Studio Worker 实际提供的 Deep Agents Runtime Profile 目录"""

from types import MappingProxyType
from typing import Literal, TypeAlias

from tinkerfin import (
    DeepAgentsRuntimeProfile,
    DeepAgentsV2RuntimeProfile,
    DeepSeekReasoningExtractor,
)

RuntimeProfileId: TypeAlias = Literal["deepagents-v2"]
RUNTIME_PROFILE_IDS: tuple[RuntimeProfileId, ...] = ("deepagents-v2",)


def build_runtime_profiles() -> MappingProxyType[str, DeepAgentsRuntimeProfile]:
    """创建 Worker 已安装且经过框架契约验证的不可变 Profile 目录

    DeepSeek extractor 只把实测 Provider 字段转为可分类的 Native reasoning
    Observation；它不授权 AG-UI 公开展示，也不授权 Trace 保留正文

    Returns:
        以稳定 Profile ID 索引的只读目录
    """

    profile = DeepAgentsV2RuntimeProfile(
        reasoning_extractors=(DeepSeekReasoningExtractor(),),
    )
    return MappingProxyType({profile.profile_id: profile})


__all__ = [
    "RUNTIME_PROFILE_IDS",
    "RuntimeProfileId",
    "build_runtime_profiles",
]
