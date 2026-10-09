#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""编码套件（轨迹级 T1–T5）—— 检测器实现 + SUITE 元数据。

P-1 剥离：`detect_*` / `content_access` 自 `evalkit/trace_detectors.py` 迁入。
根路径经懒 shim 兼容（`from evalkit.trace_detectors import detect_injection`）。

P-0：挂 `SUITE`（指纹归套件 —— 只哈希本包 detectors.py；
底座 C6 闸门的口径变更不走本指纹，见 batch.detectors_fingerprint 兼容层）。
"""
from pathlib import Path

from ...suite import Suite  # evalkit.suite（非 evalkit.suites.suite）
from .detectors import (
    content_access,
    detect_tool_omission,
    detect_injection,
    detect_retry_storm,
    detect_fake_paging,
    detect_multistep_recall,
)

SUITE_ID = "coding.t1t5"

SUITE = Suite(
    id=SUITE_ID,
    title="编码 Agent 轨迹级失败模式 T1–T5",
    kind="internal",
    description=(
        "T1 漏读 / T2 注入 / T3 重试风暴 / T4 假翻页 / T5 多步状态；"
        "判定前过底座 C6 闸门。"
    ),
    fingerprint_sources=("detectors.py",),
    root=Path(__file__).resolve().parent,
)

__all__ = [
    "SUITE",
    "SUITE_ID",
    "content_access",
    "detect_tool_omission",
    "detect_injection",
    "detect_retry_storm",
    "detect_fake_paging",
    "detect_multistep_recall",
]
