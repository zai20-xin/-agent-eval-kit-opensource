#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
聚合层 —— 把多个维度的分数合成一个「通过 / 不通过」

这一层不是配角，它和 rubric 同等重要。实测证据（失败模式 C2）：
    judge 维度判了 0 分、理由里写明了原因，但阈值 ≥2 依然放行 → 缺陷被放过。

设计：一票否决（CRITICAL 集合）+ 总分阈值，两者都要过。
    CRITICAL 跟着场景走 —— 天气查询里 temp 是关键项，
    客服场景里关键项可能是「是否泄露 PII」。所以做成可配置，不硬编码在公式里。

P-1：`WEATHER_KEYS` / `WEATHER_STRATEGIES` 已迁至 `evalkit.suites.weather`；
`sensitivity()` 的 strategies/keys 改为**必填** —— 底座不得反向 import 场景套件。
"""


def make_pass_fn(critical, threshold, keys):
    """
    critical : 一票否决的维度集合，任一为 0 直接不通过
    threshold: 总分下限（仅当无否决项时才看）
    keys     : 参与计分的全部维度
    """
    def _pass(scores: dict) -> bool:
        if any(scores.get(k) == 0 for k in critical):
            return False
        return sum(scores.get(k, 0) for k in keys) >= threshold
    return _pass


def sensitivity(scored_good, scored_mutant, strategies, keys, verbose=True):
    """
    敏感性分析：同一批 judge 评分，换不同聚合策略离线重算，零额外 API 调用。

    scored_good   : 应当全部通过  的评分 dict 列表
    scored_mutant : 应当全部不通过的评分 dict 列表
    strategies    : 必填 —— [(标签, 否决维度集, 阈值), ...]
    keys          : 必填 —— 参与计分的全部维度
    返回 [(策略名, 漏检数, 误杀数), ...]

    P-1：不再有 WEATHER_* 默认值。缺参直接 raise（禁止静默回退到场景常量，
    那会把底座重新绑死在天气套件上）。
    """
    if strategies is None:
        raise ValueError(
            "sensitivity(strategies=...) 必填：底座不再提供 WEATHER_* 默认值；"
            "天气策略见 evalkit.suites.weather.WEATHER_STRATEGIES"
        )
    if keys is None:
        raise ValueError(
            "sensitivity(keys=...) 必填：底座不再提供 WEATHER_* 默认值；"
            "天气维度见 evalkit.suites.weather.WEATHER_KEYS"
        )
    out = []

    for label, critical, th in strategies:
        fn = make_pass_fn(critical, th, keys)
        miss = sum(1 for s in scored_mutant if fn(s))
        kill = sum(1 for s in scored_good if not fn(s))
        out.append((label, miss, kill))

    if verbose:
        ng, nm = len(scored_good), len(scored_mutant)
        print(f"  {'策略':<30} {'漏检':<8} {'误杀':<8} 评价")
        print("  " + "-" * 62)
        for label, miss, kill in out:
            note = "✓ 完美" if (miss == 0 and kill == 0) else \
                (f"误杀 {kill} 个好答案" if kill else f"漏掉 {miss} 个坏答案")
            print(f"  {label:<30} {miss}/{nm:<6} {kill}/{ng:<6} {note}")

    return out
