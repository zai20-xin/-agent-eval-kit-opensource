#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""天气套件（W1 答案级）—— 场景检测器 + 聚合策略 + 统一入口 + SUITE。

P-1 剥离：自 `evalkit/detectors.py` / `evalkit/aggregate.py` 的 WEATHER_* /
根目录 `run_eval.py` 收拢到本包；对外经 `evalkit` 懒门面继续导出旧名。
"""
from pathlib import Path

from ...suite import Suite  # evalkit.suite（非 evalkit.suites.suite）
from .detectors import check_field, check_temp, check_tool_called, check_tool_args
from .strategies import WEATHER_KEYS, WEATHER_STRATEGIES

SUITE_ID = "weather.w1"

SUITE = Suite(
    id=SUITE_ID,
    title="单步天气查询（答案级 W1）",
    kind="internal",
    description="确定性 field/temp + LLM umbrella/weather/logic + 一票否决聚合。",
    fingerprint_sources=("detectors.py", "strategies.py"),
    root=Path(__file__).resolve().parent,
)

__all__ = [
    "SUITE",
    "SUITE_ID",
    "check_field",
    "check_temp",
    "check_tool_called",
    "check_tool_args",
    "WEATHER_KEYS",
    "WEATHER_STRATEGIES",
]
