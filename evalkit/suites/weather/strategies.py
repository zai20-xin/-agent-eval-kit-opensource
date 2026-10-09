#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""天气场景的维度全集与候选聚合策略（敏感性分析用）。

P-1 剥离：自 `evalkit/aggregate.py` 迁入 —— 底座聚合层不再绑定场景维度；
`aggregate.sensitivity` 的 strategies/keys 一律由调用方显式传入。
"""

WEATHER_KEYS = ("weather", "temp", "umbrella", "field", "logic")

WEATHER_STRATEGIES = [
    ("S0 纯阈值≥3", set(), 3),
    ("S1 否决(temp,logic)", {"temp", "logic"}, 3),
    ("S2 否决(temp,umbrella,logic)", {"temp", "umbrella", "logic"}, 3),
    ("S3 否决(temp,umbrella,field,logic)", {"temp", "umbrella", "field", "logic"}, 3),
    ("S4 全否决+阈值4", set(WEATHER_KEYS), 4),
]

__all__ = ["WEATHER_KEYS", "WEATHER_STRATEGIES"]
