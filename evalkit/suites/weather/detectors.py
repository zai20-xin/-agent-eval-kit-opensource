#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确定性检测器（**答案级** · 天气场景）—— 能用代码判的，绝不用 LLM 判。

★ 粒度与适用边界：
    本模块评的是**单条最终答案**（输入 = 答案文本 + ground truth），
    与 `suites/coding/detectors.py` 的**轨迹级**（输入 = NormalizedTrace）
    是不同粒度，**不是新旧替代关系**。怎么选：
        · 评「这句话答得对不对」                   → 用本模块
        · 评「这条 agent 轨迹有没有漏读/中招/死循环」 → 用 coding 轨迹检测器
    本模块**硬绑天气场景**：UNIT_TO_FIELD 只映射温度，check_temp 就是气温专用。
    新任务要复用这里的思路，请把场景**参数化**，不要直接套用。

这一层的全部价值：
    · 零方差    同样的输入永远同样的输出
    · 零 API    不花钱、不限流、不超时
    · 可单测    这段逻辑能写单元测试，judge 不行
    · 可解释    违规时能指出「出现『昨天』」，judge 只给个 0

设计原则：
    每个检测器只管一类事，职责不交叉。
    field 只管「字段存不存在」，不管「数值对不对」（那是 temp 的事）。

P-1 剥离：自 `evalkit/detectors.py` 迁入；根路径经懒 shim 继续兼容。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 通用配置

# 数字 + 单位（用于抽出所有事实性数据）
NUM_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(°C|℃|摄氏度|度|%|级|km/h|m/s|mm)", re.I)

# 单位 → 字段名。映射不到的单位 = 该字段不在 ground truth 里 = 编造
UNIT_TO_FIELD = {
    "°c": "temp", "℃": "temp", "度": "temp", "摄氏度": "temp",
    # %（湿度）和 级（风力）故意不映射 —— 天气工具不返回这两项
}

# 时间限定词：工具通常只描述"当前"，出现其他时间维度 = 编造
DEFAULT_TIME_WORDS = [
    "昨天", "昨日", "前天", "上周", "上週", "上月",
    "明天", "明日", "后天", "去年同期", "往年",
]

# 温度：必须带温度单位，否则「风力10级」的 10 会被误当气温
TEMP_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:°C|℃|摄氏度|度)")


# ---------------------------------------------------------------- 检测器

def check_field(answer: str, ground_truth: dict, time_words=None):
    """
    字段合规：答案里出现的每个「数字+单位」，其字段都必须来自 ground truth。
    同时检查是否引入了工具未覆盖的时间维度。

    返回 (score, violations)   score: 1=合规 0=存在编造
    """
    time_words = DEFAULT_TIME_WORDS if time_words is None else time_words
    violations = []

    # 1) 时间维度
    for w in time_words:
        if w in answer:
            violations.append(f"时间编造: 出现「{w}」，工具只返回了当前数据")

    # 2) 数据字段
    seen = set()
    for m in NUM_UNIT_RE.finditer(answer):
        num, unit = m.group(1), m.group(2).lower()
        field = UNIT_TO_FIELD.get(unit)
        if field is None or field not in ground_truth:
            if unit not in seen:
                seen.add(unit)
                violations.append(f"GT 未返回的字段: 「{num}{unit}」")

    return (0 if violations else 1), violations


def check_temp(answer: str, gt_temp: float, tol: float = 1.0):
    """
    气温合规：答案里出现的每个温度值，都必须落在 [gt_temp-tol, gt_temp+tol]。
    一个都没提到 = 缺失 = 不合格。

    返回 (score, values, diagnosis)
    """
    vals = [float(m.group(1)) for m in TEMP_RE.finditer(answer)]
    if not vals:
        return 0, [], "未提及气温"
    bad = [v for v in vals if abs(v - gt_temp) > tol]
    if bad:
        shown = [int(v) if float(v).is_integer() else v for v in bad]
        lo, hi = gt_temp - tol, gt_temp + tol
        return 0, vals, f"气温 {shown} 超出允许区间 [{int(lo)}, {int(hi)}]"
    return 1, vals, ""


def check_tool_called(trace: dict):
    """
    工具调用：该调的工具调了没有。（A1 失败模式）

    trace 形如 {"tool_called": bool, "tool_name": str|None, "tool_args": dict|None}
    """
    if not trace.get("tool_called"):
        return 0, ["未调用任何工具"]
    return 1, []


def check_tool_args(trace: dict, expected: dict):
    """
    工具参数：传对了吗。（A9 类失败模式，尚未实测到样本）
    """
    if not trace.get("tool_called"):
        return 0, ["未调用工具，无法校验参数"]
    args = trace.get("tool_args") or {}
    bad = {k: (args.get(k), v) for k, v in expected.items() if args.get(k) != v}
    if bad:
        detail = ", ".join(f"{k}: 实际{av} 应为{ev}" for k, (av, ev) in bad.items())
        return 0, [f"参数不符: {detail}"]
    return 1, []


__all__ = [
    "check_field",
    "check_temp",
    "check_tool_called",
    "check_tool_args",
    "NUM_UNIT_RE",
    "UNIT_TO_FIELD",
    "DEFAULT_TIME_WORDS",
    "TEMP_RE",
]
