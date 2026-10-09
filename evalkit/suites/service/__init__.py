#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客服套件（M1-A4b 注册，检测器实现落 B3）。

SUITE 注册后 `discover_suites()` 即可发现 `service.w1`；
`fingerprint_sources=("detectors.py",)` —— 判定口径指纹只归本包，
B3 新增检测器改了 detectors.py，指纹自动变（compare 可比性检查会拦）。

检测器分工（M1-B3 落地，详因见 detectors.py 模块 docstring）：
    新写 5 个 —— detect_cs_tool_omission / detect_cs_page_miss /
                 detect_cs_write_mismatch / detect_cs_pii_leak / detect_cs_privilege
    复用 3 个 —— detect_injection / detect_retry_storm / detect_multistep_recall
                 （经 TD 懒映射原名引用，实现不复制）
    实测不可复用 2 个（有据）—— detect_tool_omission 抽不出 order_id 必假阳性；
                 detect_fake_paging 看不见单次漏翻页 → 均改 cs 前缀新写
"""
from pathlib import Path

from ...suite import Suite  # evalkit.suite（非 evalkit.suites.suite）

SUITE_ID = "service.w1"

SUITE = Suite(
    id=SUITE_ID,
    title="智能客服场景（轨迹级，M1 台阶1）",
    kind="internal",
    description=(
        "客服外壳（bare scenes.customer_service）+ 失败模式导向任务集；"
        "判定前过底座 C6 闸门；检测器见 detectors.py。"
    ),
    fingerprint_sources=("detectors.py",),
    root=Path(__file__).resolve().parent,
)

__all__ = ["SUITE", "SUITE_ID"]
