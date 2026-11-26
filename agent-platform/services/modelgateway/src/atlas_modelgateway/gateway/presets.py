"""预置规则兼容导出：实际定义已下沉至 shared.presets，保留此模块供网关内部与测试按原路径导入。"""

from ..shared.presets import (
    CONSERVATIVE,
    EFFORT_LEVELS,
    PRESETS,
    Preset,
    PresetRules,
    UpstreamRules,
)

__all__ = ["CONSERVATIVE", "EFFORT_LEVELS", "PRESETS", "Preset", "PresetRules", "UpstreamRules"]
