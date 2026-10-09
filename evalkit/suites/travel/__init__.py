#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""旅游套件（M1-C2 注册）。

SUITE 注册后 `discover_suites()` 即可发现 `travel.t1`；
`fingerprint_sources=("detectors.py",)` —— 判定口径指纹只归本包，
改了 travel/detectors.py 指纹自动变（compare 可比性检查会拦）。

检测器分工（详因见 detectors.py 模块 docstring）：
    新写 5 个 —— detect_travel_hallucinated_field / detect_travel_itinerary_omission /
                 detect_travel_state_drift / detect_travel_soldout（+矩阵 P2 一器两 clause）
    跨套件复用 3 个 —— detect_cs_page_miss / detect_cs_write_mismatch / detect_injection
                 （任务 detector 字段直接引用原名，实现不复制）
"""
from pathlib import Path

from ...suite import Suite

SUITE_ID = "travel.t1"

SUITE = Suite(
    id=SUITE_ID,
    title="智能旅游场景（轨迹级，M1 台阶2）",
    kind="internal",
    description=(
        "旅游外壳（bare scenes.travel）+ 失败模式导向任务集；"
        "判定前过底座 C6 闸门；检测器见 detectors.py。"
    ),
    fingerprint_sources=("detectors.py",),
    root=Path(__file__).resolve().parent,
)

__all__ = ["SUITE", "SUITE_ID"]
