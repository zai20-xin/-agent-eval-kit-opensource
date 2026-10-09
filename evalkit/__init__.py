#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evalkit —— Agent 评测工具包（**懒门面** · PEP 562）

========================== 分层（P-1 剥离后） ==========================

  底座（本包根 · 场景无关）
    judge.py / aggregate.py / stats.py / batch.py / tasks.py / cli.py
    outcome.py              双字段结果 RunOutcome（score + metric/raw/ci）
    suite.py                Suite 元数据（指纹归套件 · SWE 预留）
    trace_schema.py          数据契约 NormalizedTrace
    trace_detectors.py       C6 有效性闸门（trace_quality / is_trace_valid）
    adapters/                换 agent 只加一个 adapter
    detectors.py             懒 shim → suites.weather（旧导入路径兼容）

  套件（evalkit.suites.* · 场景实现）
    suites/discover_suites() 扫描子包模块级 SUITE（P-0；惰性，cli 不自动调）
    suites/weather/          答案级 W1：detectors + strategies + run_eval
    suites/coding/           轨迹级 T1–T5：detect_* + content_access
    suites/tau.py            τ A 桥接（每 domain 一 tau.*，只读 reward）
    suites/swe.py            SWE-Bench A 桥接（swe.bench，只读 resolved/reward，trace=None）

  铁律：能用确定性代码判的，绝不用 LLM 判。

本模块经 PEP 562 模块级 `__getattr__` 延迟解析公共名：
    `import evalkit` 与 `import evalkit.cli` **不加载任何 suites 实现**；
    首次 `evalkit.<name>` / `from evalkit import <name>` 时才 import 对应模块。
"""
from __future__ import annotations

import importlib

__version__ = "0.3.0"

# ---- 公共名 → 实现模块（相对本包）；懒加载，禁止在模块体里 eager import suites ----
_LAZY_MAP = {
    # 答案级检测器（weather 套件）
    "check_field": ".suites.weather.detectors",
    "check_temp": ".suites.weather.detectors",
    "check_tool_called": ".suites.weather.detectors",
    "check_tool_args": ".suites.weather.detectors",
    "WEATHER_KEYS": ".suites.weather.strategies",
    "WEATHER_STRATEGIES": ".suites.weather.strategies",
    # 语义层 / 聚合层（底座）
    "get_client": ".judge",
    "judge": ".judge",
    "parse_json_loose": ".judge",
    "DEFAULT_JUDGE_MODELS": ".judge",
    "make_pass_fn": ".aggregate",
    "sensitivity": ".aggregate",
    # 数据契约
    "NormalizedTrace": ".trace_schema",
    "Step": ".trace_schema",
    "ToolCall": ".trace_schema",
    # C6 闸门（底座）
    "is_trace_valid": ".trace_detectors",
    "trace_quality": ".trace_detectors",
    # 轨迹级检测器（coding 套件；根路径 shim 兼容）
    "content_access": ".suites.coding.detectors",
    "detect_tool_omission": ".suites.coding.detectors",
    "detect_injection": ".suites.coding.detectors",
    "detect_retry_storm": ".suites.coding.detectors",
    "detect_fake_paging": ".suites.coding.detectors",
    "detect_multistep_recall": ".suites.coding.detectors",
    # 通用
    "wilson": ".stats",
    "rate_with_ci": ".stats",
    # P-0 结果契约 / 套件元数据（底座，零 suites 实现依赖）
    "RunOutcome": ".outcome",
    "clamp01": ".outcome",
    "score_from_failure_rate": ".outcome",
    "case_summary_from_outcome": ".outcome",
    "Suite": ".suite",
    "sha256_files": ".suite",
    "discover_suites": ".suites",
}

# 分组导出名（文档 / `__all__` 用；取值仍走 __getattr__）
ANSWER_LEVEL = [
    "check_field", "check_temp", "check_tool_called", "check_tool_args",
    "get_client", "judge", "parse_json_loose", "DEFAULT_JUDGE_MODELS",
    "make_pass_fn", "sensitivity", "WEATHER_KEYS", "WEATHER_STRATEGIES",
]
TRACE_LEVEL = [
    "NormalizedTrace", "Step", "ToolCall",
    "is_trace_valid", "trace_quality", "content_access",
    "detect_tool_omission", "detect_injection",
    "detect_retry_storm", "detect_fake_paging", "detect_multistep_recall",
]
COMMON = ["wilson", "rate_with_ci"]
P0 = [
    "RunOutcome", "clamp01", "score_from_failure_rate",
    "case_summary_from_outcome", "Suite", "sha256_files", "discover_suites",
]

__all__ = ANSWER_LEVEL + TRACE_LEVEL + COMMON + P0


def __getattr__(name: str):
    if name in _LAZY_MAP:
        mod = importlib.import_module(_LAZY_MAP[name], __name__)
        val = getattr(mod, name)
        globals()[name] = val  # 缓存
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_LAZY_MAP) | set(__all__))
