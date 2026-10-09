#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评测套件（内容层）—— 底座 `evalkit/*` 根之外的场景实现。

P-1（2026-09-23 剥离）：
    weather/  答案级天气场景（W1）：detectors + strategies + run_eval
    coding/   轨迹级编码 Agent 失败模式 T1–T5：detect_* + content_access

P-0 发现机制：`discover_suites()` 扫描本包子模块的模块级 `SUITE`。
**惰性**：只在显式调用 discover 时 import 子包 —— 保持
`import evalkit.cli` 不连带加载内容实现（硬验收不变）。

P-2+（2026-09-24）：
    tau.py    τ-bench A 桥接（每 domain 一 `tau.*`，只读 reward）
    swe.py    SWE-Bench A 桥接（`swe.bench`，只读 resolved/reward，trace=None）
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING, Dict

if TYPE_CHECKING:
    from ..suite import Suite


def discover_suites() -> Dict[str, "Suite"]:
    """扫描 `evalkit.suites.*` 的模块级 `SUITE` / `SUITES` → `{id: Suite}`。

    · `SUITE`  —— 单套件（weather / coding）
    · `SUITES` —— 一模块多套件（tau：每 domain 一 `tau.*`）
    加一个套件 = 在 suites/ 下建模块/子包并定义 `SUITE` 或 `SUITES`，
    **不改底座代码**。重复 id 后者覆盖前者并（此处不静默）—— 调用方如需严格可自行检查。
    """
    from ..suite import Suite, suite_from_module

    out: Dict[str, "Suite"] = {}
    for m in pkgutil.iter_modules(__path__):
        if m.name.startswith("_"):
            continue
        mod = importlib.import_module(f".{m.name}", __name__)
        s = suite_from_module(mod)
        if s is not None:
            out[s.id] = s
        # P-1 τ：一模块多套件（dict[str, Suite] 或 list/tuple[Suite]）
        multi = getattr(mod, "SUITES", None)
        if isinstance(multi, dict):
            for k, v in multi.items():
                if isinstance(v, Suite):
                    out[v.id or k] = v
        elif isinstance(multi, (list, tuple)):
            for v in multi:
                if isinstance(v, Suite):
                    out[v.id] = v
    return out


__all__ = ["discover_suites"]
