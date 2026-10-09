#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""答案级确定性检测器 —— **懒 shim**（实现已迁至 `evalkit.suites.weather`）。

P-1（2026-09-23 剥离）：
    实现体在 `evalkit/suites/weather/detectors.py`（天气场景硬绑）。
    本模块经 PEP 562 模块级 `__getattr__` 延迟转发，保持旧导入路径：

        from evalkit.detectors import check_temp          # 仍可用（实验03）
        from evalkit import check_field                   # 经 evalkit 懒门面

    `import evalkit.detectors` 不会加载实现；首次取属性时才 import weather。
"""
from __future__ import annotations

_LAZY_ATTRS = frozenset({
    "check_field", "check_temp", "check_tool_called", "check_tool_args",
    "NUM_UNIT_RE", "UNIT_TO_FIELD", "DEFAULT_TIME_WORDS", "TEMP_RE",
})

# 属性名 → 实现子模块（相对本包）
_LAZY_MOD = ".suites.weather.detectors"


def __getattr__(name: str):
    if name in _LAZY_ATTRS:
        import importlib
        mod = importlib.import_module(_LAZY_MOD, __package__)
        val = getattr(mod, name)
        globals()[name] = val  # 缓存，后续直取
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | _LAZY_ATTRS)


__all__ = [
    "check_field", "check_temp", "check_tool_called", "check_tool_args",
]
