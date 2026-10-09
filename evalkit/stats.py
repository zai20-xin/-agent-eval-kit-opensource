#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统计与区间估计 —— 通用层。

为什么单列一层：
    率值（如「30 次里 15 次中招 = 50%」）单独看没有说服力，必须带置信区间，
    否则小样本的 1/3 会被当成 33% 的真值去用。本模块只做纯计算：
    零依赖、零 API、可单测。

2026-09-21 并层：原先 wilson() 内联写在
    experiments/实验08-6agent横评/harness/phase1_judge.py
里，属于「每个实验各写一份」的重复实现 → 提到公共层，全项目只留一个。
"""
from __future__ import annotations

import math


def wilson(k, n, z=1.96):
    """Wilson score interval（率的置信区间）。

    k: 命中数 ｜ n: 样本数 ｜ z: 1.96 = 95%
    返回 (p, lo, hi)：点估计 + 下界 + 上界，均归一化到 [0, 1]。

    为什么不用「p ± 1.96·sqrt(p(1-p)/n)」（Wald 区间）：
        小样本或 p 接近 0/1 时 Wald 区间会越界 —— 0/30 会给出 [0%, 0%]，
        看着像「确信是 0」，而 Wilson 在同样数据下给 [0%, 11.4%]，才是诚实的表述。

    实测出处：本函数输出的区间值即
        experiments/实验08-6agent横评/results/opencode/phase1/summary_phase1_k30.txt
    """
    if n == 0:
        return 0.0, 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, max(0.0, center - half), min(1.0, center + half)


def rate_with_ci(k, n, digits=1, z=1.96):
    """把 wilson() 包成一行可读字符串。

    例：rate_with_ci(15, 30) → '15/30=50.0% CI[33.2%,66.8%]'

    仅作便利函数 —— 各实验自有的汇总格式（含失败语义标签）仍可自行拼接，
    但区间必须来自 wilson()，不要再手写公式。
    """
    p, lo, hi = wilson(k, n, z)
    return (f"{k}/{n}={p * 100:.{digits}f}% "
            f"CI[{lo * 100:.{digits}f}%,{hi * 100:.{digits}f}%]")


def two_proportion_z(k1, n1, k2, n2):
    """两比例 z 检验（合并方差、无连续性校正）→ (z, 双尾 p)。

    用途：判断**同一 case 在两个批次间的差异**是否超出抽样噪声。
    没有它，跨批次对比就只能看数字大小 —— 而小样本下「1/3 vs 0/3」看着差很多，
    实际完全可能是噪声。

    ⚠️ 正态近似的前提是小样本不成立：**n < 10 的格子不看 p 值**，只报原始计数。
       本函数不替使用者判断，调用方必须自己把关（在输出里显式标注）。
    """
    if n1 == 0 or n2 == 0:
        return 0.0, 1.0
    p1, p2 = k1 / n1, k2 / n2
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0, 1.0
    z = (p1 - p2) / se
    return z, math.erfc(abs(z) / math.sqrt(2))
